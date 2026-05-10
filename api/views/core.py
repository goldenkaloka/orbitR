from apps.orders.models import Dispute
from django.db import models
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.products.models import Food
from apps.orders.models import Order
from apps.delivery.models import Driver
from apps.wallets.models import Wallet
from apps.core.models import Notification
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3, get_neighbors
from apps.core.cache_keys import stable_cache_key

from api.serializers.core import (
    UserSerializer, RestaurantSerializer, OrderSerializer, 
    DriverSerializer, FoodSerializer, NotificationSerializer,
    DisputeSerializer, RestaurantListSerializer
)

from django.core.cache import cache
from domain.services.order_services import (
    ConfirmOrderService, 
    StartPreparingService,
    MarkReadyService,
    AssignDriverService, 
    PickupOrderService,
    DeliverOrderService
)
from domain.fsm.order_fsm import InvalidStateTransitionError

class UserViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=['GET'])
    def me(self, request):
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)

    @action(detail=False, methods=['GET'])
    def search(self, request):
        query = request.query_params.get('q', '')
        if len(query) < 3:
            return Response([])
        
        # Optimize for "billions": istartswith is faster than icontains on indexed fields
        # Also limit results and only return relevant fields if necessary
        users = User.objects.filter(
            models.Q(username__icontains=query) | 
            models.Q(phone__istartswith=query)
        ).only('id', 'username', 'phone', 'role').order_by('username')[:10]
        
        serializer = self.get_serializer(users, many=True)
        return Response(serializer.data)

class FoodViewSet(viewsets.ModelViewSet):
    """
    Menu management for vendors.
    Customers see available foods, vendors manage their own foods.
    """
    serializer_class = FoodSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Food.objects.select_related('vendor', 'vendor__owner').all()
        if user.is_authenticated and user.role == 'vendor':
            return queryset.filter(vendor__owner=user)
        return queryset.filter(is_available=True)

    def perform_create(self, serializer):
        from domain.services.menu_services import MenuService
        try:
            serializer.instance = MenuService.create_food(
                owner_user=self.request.user,
                name=serializer.validated_data['name'],
                price=serializer.validated_data['price'],
                description=serializer.validated_data.get('description', ''),
                category=serializer.validated_data.get('category', 'MAIN'),
                is_available=serializer.validated_data.get('is_available', True),
                image=serializer.validated_data.get('image')
            )
        except ValueError as e:
            from rest_framework.exceptions import ValidationError
            raise ValidationError(str(e))

    def perform_destroy(self, instance):
        from domain.services.menu_services import MenuService
        try:
            MenuService.delete_food(owner_user=self.request.user, food_id=instance.id)
        except ValueError as e:
            from rest_framework.exceptions import ValidationError
            raise ValidationError(str(e))

class RestaurantViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RestaurantSerializer

    def get_serializer_class(self):
        # Avoid heavy nested payloads on list endpoints.
        if getattr(self, "action", None) == "list":
            return RestaurantListSerializer
        return RestaurantSerializer

    def get_queryset(self):
        queryset = Restaurant.objects.select_related('owner').prefetch_related('foods').filter(is_active=True)
        if getattr(self, "action", None) in ("retrieve", "mine"):
            # Already prefetched above for all actions
            pass
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category__iexact=category)
            
        # H3 Proximity Filtering
        lat = self.request.query_params.get('lat')
        lng = self.request.query_params.get('lng')
        if lat and lng:
            user_h3 = latlng_to_h3(lat, lng, ZONE_RESOLUTION)
            if user_h3:
                # Include neighbors for a better user experience
                neighbor_cells = get_neighbors(user_h3, 1)
                if neighbor_cells:
                    queryset = queryset.filter(h3_index__in=neighbor_cells)
        return queryset

    def list(self, request, *args, **kwargs):
        category = request.query_params.get('category', 'all').lower()
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        user_h3 = latlng_to_h3(lat, lng, ZONE_RESOLUTION) if (lat and lng) else None
        limit = request.query_params.get('limit')
        offset = request.query_params.get('offset')

        cache_key = stable_cache_key(
            "restaurant_list",
            {
                "category": category,
                "h3": user_h3 or "none",
                "limit": limit or "default",
                "offset": offset or "0",
            },
        )
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        
        response = super().list(request, *args, **kwargs)
        cache.set(cache_key, response.data, 900) # Cache for 15 minutes
        return response

    @action(detail=False, methods=['GET'])
    def mine(self, request):
        """Get the restaurant owned by the current user."""
        cache_key = f"restaurant_mine_{request.user.id}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        try:
            restaurant = Restaurant.objects.select_related('owner').prefetch_related('foods').get(owner=request.user)
            data = RestaurantSerializer(restaurant).data
            cache.set(cache_key, data, 3600) # Cache for 1 hour
            return Response(data)
        except Restaurant.DoesNotExist:
            return Response({}, status=200)

    @action(detail=True, methods=['POST'], url_path='register-temp-driver')
    def register_temp_driver(self, request, pk=None):
        """
        Register a user as a temporary driver for this restaurant.
        Body: { user_id }
        """
        user_id = request.data.get('user_id')
        if not user_id:
            return Response({'error': 'user_id is required'}, status=400)

        restaurant = self.get_object()
        if restaurant.owner != request.user:
            return Response({'error': 'Only the restaurant owner can register temp drivers'}, status=403)

        try:
            from domain.services.admin_services import RegisterTempDriverService
            driver = RegisterTempDriverService.execute(user_id=user_id, restaurant_id=str(restaurant.id))
            return Response(DriverSerializer(driver).data)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['GET'], url_path='delivery-fee')
    def delivery_fee(self, request, pk=None):
        """
        Calculate delivery fee for a specific restaurant and location.
        Query params: lat, lng
        """
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        
        if not lat or not lng:
            return Response({'error': 'lat and lng are required'}, status=400)
            
        restaurant = self.get_object()
        from django.contrib.gis.geos import Point
        try:
            point = Point(float(lng), float(lat))
            from domain.services.pricing_service import PricingService
            fee = PricingService.calculate_delivery_fee(restaurant, point)
            return Response({
                'restaurant_id': restaurant.id,
                'delivery_fee': float(fee),
                'currency': 'TSh'
            })
        except (ValueError, TypeError):
            return Response({'error': 'Invalid lat or lng'}, status=400)

class OrderViewSet(viewsets.ModelViewSet):
    queryset = Order.objects.all()
    serializer_class = OrderSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        serializer.save(customer=self.request.user)

    def get_queryset(self):
        user = self.request.user
        queryset = Order.objects.select_related(
            'customer', 
            'restaurant', 
            'restaurant__owner', 
            'driver', 
            'driver__user'
        ).prefetch_related('items', 'items__food').all()
        
        if user.role == 'customer':
            return queryset.filter(customer=user)
        elif user.role == 'vendor':
            # Vendors only see paid orders
            return queryset.filter(restaurant__owner=user, is_paid=True)
        elif user.role == 'driver':
            return queryset.filter(driver__user=user, is_paid=True)
        return queryset.filter(is_paid=True)

    @action(detail=False, methods=['GET'])
    def stats(self, request):
        cache_key = f"user_stats_{request.user.id}_{request.user.role}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        from django.db.models import Sum, Count
        from django.utils import timezone
        now = timezone.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timezone.timedelta(days=1)
        
        user = request.user
        wallet, _ = Wallet.objects.get_or_create(user=user)
        
        data = {}
        try:
            if user.role == 'vendor':
                orders = Order.objects.filter(
                    restaurant__owner=user,
                    created_at__gte=start_of_day,
                    created_at__lt=end_of_day,
                    is_paid=True,
                )
                revenue = orders.filter(status='DELIVERED').aggregate(Sum('total_amount'))['total_amount__sum'] or 0
                count = orders.count()
                data = {
                    "revenue_today": float(revenue),
                    "orders_today": count,
                    "avg_prep_time": "08:45",
                    "wallet_balance": float(wallet.balance),
                    "pending_payout": float(wallet.pending_balance)
                }
            elif user.role == 'driver':
                orders = Order.objects.filter(
                    driver__user=user,
                    created_at__gte=start_of_day,
                    created_at__lt=end_of_day,
                    is_paid=True,
                )
                earnings = orders.filter(status='DELIVERED').aggregate(Sum('delivery_fee'))['delivery_fee__sum'] or 0
                count = orders.count()
                data = {
                    "earnings_today": float(earnings),
                    "trips_today": count,
                    "online_time": "04:20",
                    "wallet_balance": float(wallet.balance),
                    "status": user.driver_profile.status
                }
            else:
                return Response({"error": "Stats not available for this role"}, status=400)
            
            cache.set(cache_key, data, 300)
            return Response(data)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

    @action(detail=False, methods=['GET'], url_path='available-jobs')
    def available_jobs(self, request):
        # Orders that are READY, PAID, and don't have a driver assigned
        limit = int(request.query_params.get('limit', 20))
        offset = int(request.query_params.get('offset', 0))
        
        # RESTORED: Filter by driver's current H3 cell if available
        try:
            driver = request.user.driver_profile
            if driver.h3_index:
                # Show jobs in driver's cell and immediate neighbors
                neighbor_cells = get_neighbors(driver.h3_index, 1)
                if neighbor_cells:
                    queryset = Order.objects.filter(h3_index__in=neighbor_cells)
                else:
                    queryset = Order.objects.all()
            else:
                queryset = Order.objects.all()
        except Driver.DoesNotExist:
            queryset = Order.objects.all()

        orders = queryset.select_related(
            'restaurant', 
            'restaurant__owner', 
            'customer'
        ).prefetch_related('items', 'items__food').filter(
            status='READY', 
            is_paid=True, 
            driver__isnull=True
        ).order_by('-created_at')[offset:offset+limit]
        
        return Response(OrderSerializer(orders, many=True).data)

    @action(detail=True, methods=['POST'], url_path='claim-job')
    def claim_job(self, request, pk=None):
        from django.db import transaction
        try:
            with transaction.atomic():
                driver = Driver.objects.select_for_update().get(user=request.user)
                # Security: Lock order and check if it's READY for claiming
                order = Order.objects.select_for_update().get(id=pk)
                if order.status != 'READY':
                    return Response({"error": f"Cannot claim job in {order.status} state"}, status=400)
                
                order = AssignDriverService.execute(pk, driver.id, request.user)
                
                # Track successful claim attempt
                driver.total_claim_attempts += 1
                driver.save(update_fields=['total_claim_attempts'])
                
                return Response(OrderSerializer(order).data)
        except Driver.DoesNotExist:
            return Response({"error": "You are not registered as a driver"}, status=403)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)
        except Exception as e:
            return Response({"error": f"Claim failed: {str(e)}"}, status=409)

    @action(detail=True, methods=['POST'])
    def confirm(self, request, pk=None):
        try:
            # Security: Manual check if restaurant owner
            order = Order.objects.get(id=pk)
            if order.restaurant.owner != request.user:
                return Response({"error": "Not authorized to confirm this order"}, status=403)
                
            order = ConfirmOrderService.execute(pk, request.user)
            return Response(OrderSerializer(order).data)
        except (InvalidStateTransitionError, PermissionError) as e:
            return Response({"error": str(e)}, status=403 if isinstance(e, PermissionError) else 400)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

    @action(detail=True, methods=['POST'], url_path='start-preparing')
    def start_preparing(self, request, pk=None):
        try:
            # Security: Manual check if restaurant owner
            order = Order.objects.get(id=pk)
            if order.restaurant.owner != request.user:
                return Response({"error": "Not authorized to prepare this order"}, status=403)
                
            order = StartPreparingService.execute(pk, request.user)
            return Response(OrderSerializer(order).data)
        except InvalidStateTransitionError as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'])
    def ready(self, request, pk=None):
        try:
            order = MarkReadyService.execute(pk, request.user)
            return Response(OrderSerializer(order).data)
        except InvalidStateTransitionError as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='assign-driver')
    def assign_driver(self, request, pk=None):
        driver_id = request.data.get('driver_id')
        if not driver_id:
            return Response({"error": "driver_id is required"}, status=400)
        try:
            order = AssignDriverService.execute(pk, driver_id, request.user)
            return Response(OrderSerializer(order).data)
        except Exception as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'])
    def pickup(self, request, pk=None):
        bypass = request.data.get('bypass_geofence', False)
        try:
            order = PickupOrderService.execute(pk, request.user, bypass_geofence=bypass)
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (ValueError, InvalidStateTransitionError) as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'])
    def deliver(self, request, pk=None):
        otp = request.data.get('otp')
        bypass = request.data.get('bypass_geofence', False)
        
        try:
            order = DeliverOrderService.execute(pk, request.user, otp=otp, bypass_geofence=bypass)
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (ValueError, InvalidStateTransitionError) as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='rider-accept')
    def rider_accept(self, request, pk=None):
        from domain.services.order_services import AcceptOrderService
        try:
            order = AcceptOrderService.execute(pk, request.user)
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (InvalidStateTransitionError, ValueError) as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='rider-reject')
    def rider_reject(self, request, pk=None):
        from domain.services.order_services import RejectOrderService
        try:
            order = RejectOrderService.execute(pk, request.user)
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (InvalidStateTransitionError, ValueError) as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'])
    def reject(self, request, pk=None):
        try:
            from domain.services.order_services import CancelOrderService
            order = CancelOrderService.execute(pk, request.user, reason='Rejected by restaurant')
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (InvalidStateTransitionError, ValueError) as e:
            return Response({"error": str(e)}, status=400)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

    @action(detail=True, methods=['POST'], url_path='assign-temp-driver')
    def assign_temp_driver(self, request, pk=None):
        """
        Vendor assigns a temporary driver from a registered user.
        Body: { user_id }
        """
        user_id = request.data.get('user_id')
        if not user_id:
            return Response({'error': 'user_id is required'}, status=400)

        try:
            order = Order.objects.get(id=pk)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=404)

        # Only the restaurant owner can assign temp drivers
        if request.user.role != 'vendor' or order.restaurant.owner != request.user:
            return Response({'error': 'Only the restaurant owner can assign temp drivers'}, status=403)

        if order.status != 'READY':
            return Response({'error': 'Can only assign drivers to READY orders'}, status=400)

        try:
            target_user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=404)

        # Register temporary driver via domain service
        try:
            from domain.services.admin_services import RegisterTempDriverService
            driver = RegisterTempDriverService.execute(user_id=target_user.id, restaurant_id=order.restaurant.id)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)

        # Assign driver to order
        order = AssignDriverService.execute(pk, driver.id, request.user)
        return Response(OrderSerializer(order).data)

    @action(detail=True, methods=['POST'])
    def pay(self, request, pk=None):
        from domain.services.payment_services import InitiatePaymentService
        provider = request.data.get('provider')
        phone = request.data.get('phone')
        bank_code = request.data.get('bank_code')
        try:
            result = InitiatePaymentService.execute(pk, provider=provider, phone=phone, bank_code=bank_code)
            payment = result['payment']
            gateway_data = result.get('gateway_response', {})
            
            # Merge gateway data into response for Flutter convenience
            return Response({
                "success": True,
                "payment_id": str(payment.id), 
                "status": payment.status,
                **gateway_data
            })
        except Exception as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'])
    def dispute(self, request, pk=None):
        reason = request.data.get('reason')
        if not reason:
            return Response({"error": "reason is required"}, status=400)
        
        order = self.get_object()
        if order.customer != request.user:
            return Response({"error": "Only the customer can dispute this order"}, status=403)
            
        from domain.services.admin_services import DisputeOrderService
        dispute = DisputeOrderService.execute(order.id, request.user, reason)
            
        return Response(DisputeSerializer(dispute).data)

    @action(detail=True, methods=['POST'])
    def refund(self, request, pk=None):
        order = self.get_object()
        if order.customer != request.user:
            return Response({"error": "Only the customer can request a refund"}, status=403)
            
        from domain.services.payment_services import RefundPaymentService
        try:
            success = RefundPaymentService.execute(order.id)
            if success:
                return Response({"status": "Refund initiated successfully"})
            else:
                return Response({"error": "Refund initiation failed"}, status=400)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

class DisputeViewSet(viewsets.ModelViewSet):
    queryset = Dispute.objects.all()
    serializer_class = DisputeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        from apps.orders.models import Dispute
        if user.role == 'customer':
            return Dispute.objects.filter(raised_by=user)
        elif user.role == 'admin':
            return Dispute.objects.all()
        return Dispute.objects.none()

class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user)

    @action(detail=True, methods=['POST'])
    def read(self, request, pk=None):
        notification = self.get_object()
        notification.is_read = True
        notification.save()
        return Response({'status': 'marked as read'})

    @action(detail=False, methods=['POST'], url_path='read-all')
    def read_all(self, request):
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({'status': 'all marked as read'})


