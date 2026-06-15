import logging
logger = logging.getLogger(__name__)

from apps.orders.models import Dispute
from django.db import models
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from django.contrib.postgres.search import TrigramSimilarity

from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.products.models import Food
from apps.orders.models import Order, DeliveryBatch
from apps.delivery.models import Driver
from apps.wallets.models import Wallet, WalletTransaction
from apps.core.models import Notification
from apps.marketplace.models import MarketplaceProduct
from apps.core.geo_utils import get_neighbors
from apps.core.cache_keys import stable_cache_key

from api.serializers.core import (
    UserSerializer, RestaurantSerializer, OrderSerializer, 
    DriverSerializer, FoodSerializer, NotificationSerializer,
    DisputeSerializer, RestaurantListSerializer
)
from api.serializers.marketplace import MarketplaceProductSerializer

from django.core.cache import cache
from django.utils import timezone
from django.conf import settings
from domain.services.order_services import (
    ConfirmOrderService,
    StartPreparingService,
    MarkReadyService,
    AssignDriverService,
    AssignBatchService,
    PickupOrderService,
    DeliverOrderService,
)
from domain.fsm.order_fsm import InvalidStateTransitionError

def build_restaurant_mine_data(user):
    from apps.restaurants.models import SectionStaff
    
    try:
        restaurant = Restaurant.objects.select_related('owner').get(owner=user)
        is_owner = True
    except Restaurant.DoesNotExist:
        staff_link = SectionStaff.objects.filter(user=user).select_related('section__restaurant').first()
        if not staff_link:
            return {}
        restaurant = staff_link.section.restaurant
        is_owner = False

    if is_owner:
        data = RestaurantSerializer(restaurant).data
        data['staff_section_ids'] = []
    else:
        staff_section_ids = list(SectionStaff.objects.filter(user=user).values_list('section_id', flat=True))
        data = RestaurantSerializer(restaurant).data
        data['foods'] = [f for f in data.get('foods', []) if f.get('section') in staff_section_ids or f.get('section') is None]
        data['staff_section_ids'] = [str(sid) for sid in staff_section_ids]

    data['is_owner'] = is_owner

    pending_count = DeliveryBatch.objects.filter(
        status='PENDING_DRIVER',
        orders__restaurant_id=restaurant.id
    ).values('id').distinct().count()

    data['pending_batches_banner'] = {
        'has_pending': pending_count > 0,
        'count': pending_count,
        'message': f'{pending_count} batch(es) waiting for driver' if pending_count > 0 else None
    }

    return data

def invalidate_restaurant_cache(restaurant):
    if not restaurant:
        return
    # Pre-warm owner's cache
    if restaurant.owner_id:
        cache_key = f"restaurant_mine_{restaurant.owner_id}"
        try:
            owner = User.objects.get(id=restaurant.owner_id)
            data = build_restaurant_mine_data(owner)
            if data:
                cache.set(cache_key, data, 3600)
            else:
                cache.delete(cache_key)
        except Exception:
            cache.delete(cache_key)

    # Pre-warm all section staff's caches
    from apps.restaurants.models import SectionStaff
    staff_user_ids = SectionStaff.objects.filter(section__restaurant=restaurant).values_list('user_id', flat=True)
    for uid in staff_user_ids:
        cache_key = f"restaurant_mine_{uid}"
        try:
            user = User.objects.get(id=uid)
            data = build_restaurant_mine_data(user)
            if data:
                cache.set(cache_key, data, 3600)
            else:
                cache.delete(cache_key)
        except Exception:
            cache.delete(cache_key)

class UserViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = UserSerializer

    def get_queryset(self):
        # Security: Prevent PII leak. Regular users can only see themselves.
        # Admins should use the AdminViewSet for bulk user management.
        return User.objects.filter(id=self.request.user.id)

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
        queryset = Food.objects.select_related('section__restaurant', 'section__restaurant__owner', 'section').all()
        if user.is_authenticated and user.role == 'vendor':
            from apps.restaurants.models import SectionStaff
            if SectionStaff.objects.filter(user=user).exists():
                staff_section_ids = SectionStaff.objects.filter(user=user).values_list('section_id', flat=True)
                return queryset.filter(section__restaurant__owner=user) | queryset.filter(section_id__in=staff_section_ids)
            return queryset.filter(section__restaurant__owner=user)
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
                image=serializer.validated_data.get('image'),
                section=serializer.validated_data.get('section')
            )
            invalidate_restaurant_cache(serializer.instance.vendor)
        except ValueError as e:
            from rest_framework.exceptions import ValidationError
            raise ValidationError(str(e))

    def perform_destroy(self, instance):
        from domain.services.menu_services import MenuService
        vendor = instance.vendor
        try:
            MenuService.delete_food(owner_user=self.request.user, food_id=instance.id)
            invalidate_restaurant_cache(vendor)
        except ValueError as e:
            from rest_framework.exceptions import ValidationError
            raise ValidationError(str(e))

    def perform_update(self, serializer):
        if serializer.instance.vendor.owner != self.request.user:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('Only the restaurant owner can edit food items')
        serializer.save()
        invalidate_restaurant_cache(serializer.instance.vendor)

class RestaurantViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RestaurantSerializer

    def get_serializer_class(self):
        # Avoid heavy nested payloads on list endpoints.
        if getattr(self, "action", None) == "list":
            return RestaurantListSerializer
        return RestaurantSerializer

    def get_queryset(self):
        queryset = Restaurant.objects.select_related('owner').prefetch_related('sections__foods').filter(is_active=True)
        if getattr(self, "action", None) in ("retrieve", "mine"):
            pass
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category__iexact=category)

        # Proximity sorting — nearest first, expanding outward across all zones
        if getattr(self, "action", None) == "list":
            lat = self.request.query_params.get('lat')
            lng = self.request.query_params.get('lng')
            if lat and lng:
                from django.contrib.gis.db.models.functions import Distance
                from django.contrib.gis.geos import Point
                from django.db.models import Case, Value, FloatField, When
                user_location = Point(float(lng), float(lat), srid=4326)
                queryset = queryset.annotate(
                    distance=Case(
                        When(geo_location__isnull=False, then=Distance('geo_location', user_location)),
                        default=Value(1e10),
                        output_field=FloatField(),
                    )
                ).order_by('distance')
        return queryset

    def list(self, request, *args, **kwargs):
        category = request.query_params.get('category', 'all').lower()
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        limit = request.query_params.get('limit')
        offset = request.query_params.get('offset')

        cache_key = stable_cache_key(
            "restaurant_list",
            {
                "category": category,
                "lat": lat or "none",
                "lng": lng or "none",
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
        """Get the restaurant owned by the current user (or staff's restaurant)."""
        cache_key = f"restaurant_mine_{request.user.id}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        data = build_restaurant_mine_data(request.user)
        if not data:
            return Response({}, status=200)

        cache.set(cache_key, data, 3600)
        return Response(data)

    @action(detail=True, methods=['GET'], url_path='drivers')
    def list_drivers(self, request, pk=None):
        """List all drivers assigned to this restaurant (owner only)."""
        restaurant = self.get_object()
        if restaurant.owner != request.user:
            return Response({'error': 'Only the restaurant owner can view drivers'}, status=403)
        drivers = restaurant.drivers.select_related('user').all()
        return Response(DriverSerializer(drivers, many=True).data)

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
            delivery_location = Point(float(lng), float(lat))
            from domain.services.pricing_service import PricingService
            # Calculate dynamic fee
            fee, service_fee, fee_log = PricingService.calculate_delivery_fee(restaurant, delivery_location)
            
            # Extract breakdown from fee_log
            breakdown = {
                "base_fee": float(fee_log.base_fee_used) if fee_log else 1000.0,
                "adjustments": fee_log.adjustments if fee_log else {}
            }
            
            return Response({
                "delivery_fee": float(fee),
                "service_fee": float(service_fee),
                "currency": "TSh",
                "breakdown": breakdown
            })
        except (ValueError, TypeError):
            return Response({'error': 'Invalid lat or lng'}, status=400)

    @action(detail=False, methods=['GET'], url_path='pending-batches')
    def pending_batches(self, request):
        """
        Get batches waiting for temp driver that contain orders from this restaurant.
        Used by restaurant owner to see batches needing a driver.
        """
        from django.utils import timezone
        from django.conf import settings
        from datetime import timedelta

        try:
            restaurant = Restaurant.objects.get(owner=request.user)
        except Restaurant.DoesNotExist:
            return Response({'error': 'Restaurant not found'}, status=404)

        batches = DeliveryBatch.objects.filter(
            status='PENDING_DRIVER',
            orders__restaurant_id=restaurant.id
        ).distinct().prefetch_related('orders')

        restaurant_batches = []
        for batch in batches:
            all_orders = list(batch.orders.all())
            restaurant_orders = [o for o in all_orders if o.restaurant_id == restaurant.id]
            if restaurant_orders:
                restaurant_batches.append({
                    'batch_id': str(batch.id),
                    'status': batch.status,
                    'waiting_since': batch.waiting_since.isoformat() if batch.waiting_since else None,
                    'order_count': len(all_orders),
                    'restaurant_order_count': len(restaurant_orders),
                    'total_value': float(sum(o.total_amount for o in all_orders)),
                })

        return Response({
            'batches': restaurant_batches,
            'count': len(restaurant_batches),
            'has_pending': len(restaurant_batches) > 0
        })

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
            'driver__user',
            'batch',
        ).prefetch_related(
            'items', 'items__food',
            'pool__members',
            'pool__members__user',
        ).order_by('-created_at')
        
        if user.role == 'customer':
            return queryset.filter(customer=user)
        elif user.role == 'vendor':
            from apps.restaurants.models import SectionStaff
            staff_sections = SectionStaff.objects.filter(user=user).values_list('section_id', flat=True)
            if staff_sections.exists():
                return queryset.filter(
                    items__section_id__in=list(staff_sections),
                    is_paid=True
                ).distinct()
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
                from apps.restaurants.models import SectionStaff
                staff_sections = SectionStaff.objects.filter(user=user).values_list('section_id', flat=True)
                if staff_sections.exists():
                    orders = Order.objects.filter(
                        items__section_id__in=list(staff_sections),
                        created_at__gte=start_of_day,
                        created_at__lt=end_of_day,
                        is_paid=True,
                    ).distinct()
                else:
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
                driver = user.driver_profile
                orders = Order.objects.filter(
                    driver__user=user,
                    created_at__gte=start_of_day,
                    created_at__lt=end_of_day,
                    is_paid=True,
                )
                
                # Calculate earnings from wallet transactions (actual driver payouts)
                today_earnings = WalletTransaction.objects.filter(
                    wallet=wallet,
                    tx_type='CREDIT',
                    created_at__gte=start_of_day,
                    created_at__lt=end_of_day,
                ).aggregate(Sum('amount'))['amount__sum'] or 0
                
                delivered_count = orders.filter(status='DELIVERED').count()
                
                # Calculate online time from driver's last_heartbeat
                online_minutes = 0
                if driver.last_heartbeat:
                    online_seconds = (now - driver.last_heartbeat).total_seconds()
                    if online_seconds > 0:
                        online_minutes = int(online_seconds / 60)
                online_hours = online_minutes // 60
                online_mins = online_minutes % 60
                
                data = {
                    "earnings_today": float(today_earnings),
                    "trips_today": delivered_count,
                    "online_time": f"{online_hours:02d}:{online_mins:02d}",
                    "wallet_balance": float(wallet.balance),
                    "pending_balance": float(wallet.pending_balance),
                    "status": driver.status,
                    "total_earnings": float(driver.total_earnings or 0),
                    "today_earnings": float(driver.today_earnings or 0),
                    "rating": float(driver.rating),
                }
            else:
                return Response({"error": "Stats not available for this role"}, status=400)
            
            cache.set(cache_key, data, 300)
            return Response(data)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

    @action(detail=False, methods=['GET'], url_path='available-jobs')
    def available_jobs(self, request):
        limit = int(request.query_params.get('limit', 20))
        offset = int(request.query_params.get('offset', 0))

        try:
            driver = request.user.driver_profile
            h3_cell = driver.h3_index if driver else None
            driver_id = str(driver.id) if driver else 'none'
        except Exception:
            driver = None
            h3_cell = None
            driver_id = 'none'

        # Cache per driver and zone with 15s TTL — drivers poll this frequently
        cache_key = f"available_jobs:{driver_id}:{h3_cell or 'none'}:{limit}:{offset}"
        cached = cache.get(cache_key)
        if cached:
            return Response(cached)

        if h3_cell:
            neighbor_cells = get_neighbors(h3_cell, 1)
            zone_filter = {'h3_index__in': neighbor_cells} if neighbor_cells else {}
        else:
            zone_filter = {}

        orders = Order.objects.filter(
            **zone_filter,
            status='READY',
            is_paid=True,
            driver__isnull=True,
            batch__isnull=True,
        ).select_related(
            'restaurant', 'restaurant__owner', 'customer',
        ).prefetch_related('items', 'items__food').order_by('-created_at')[offset:offset + limit]

        data = OrderSerializer(orders, many=True).data
        cache.set(cache_key, data, 15)
        return Response(data)

    @action(detail=True, methods=['POST'], url_path='claim-job')
    def claim_job(self, request, pk=None):
        from django.db import transaction
        try:
            with transaction.atomic():
                driver = Driver.objects.select_for_update().get(user=request.user)
                order = Order.objects.select_for_update().get(id=pk)
                if order.status != 'READY':
                    return Response({"error": f"Cannot claim job in {order.status} state"}, status=400)

                if not driver.assigned_by_id or driver.assigned_by_id != order.restaurant_id:
                    return Response({"error": "Driver not assigned to this restaurant"}, status=403)

                if order.batch_id:
                    batch = AssignBatchService.execute(order.batch_id, driver.id, request.user)
                    if batch.status == 'DISPATCHED':
                        order.refresh_from_db()
                    else:
                        # Fallback to single-order assignment
                        order = AssignDriverService.execute(pk, driver.id, request.user)
                else:
                    order = AssignDriverService.execute(pk, driver.id, request.user)
                

                return Response(OrderSerializer(order).data)
        except Driver.DoesNotExist:
            return Response({"error": "You are not registered as a driver"}, status=403)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)
        except Exception as e:
            return Response({"error": f"Claim failed: {str(e)}"}, status=409)

    @action(detail=True, methods=['POST'])
    def confirm(self, request, pk=None):
        import logging
        _log = logging.getLogger(__name__)
        try:
            # Security: Manual check if restaurant owner
            order = Order.objects.get(id=pk)
            if order.restaurant.owner != request.user:
                return Response({"error": "Not authorized to confirm this order"}, status=403)
                
            _log.warning("CONFIRM_DEBUG: order=%s status=%s is_paid=%s", pk, order.status, order.is_paid)
            order = ConfirmOrderService.execute(pk, request.user)
            return Response(OrderSerializer(order).data)
        except (InvalidStateTransitionError, PermissionError) as e:
            _log.warning("CONFIRM_DEBUG error: %s", str(e))
            return Response({"error": str(e)}, status=403 if isinstance(e, PermissionError) else 400)
        except Exception as e:
            _log.warning("CONFIRM_DEBUG unexpected error: %s", str(e))
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

    @action(detail=True, methods=['POST'], url_path='rider-cancel')
    def rider_cancel(self, request, pk=None):
        reason = request.data.get('reason', 'Cancelled by driver')
        from domain.services.order_services import CancelOrderService
        try:
            order = CancelOrderService.execute(pk, request.user, reason=reason)
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (InvalidStateTransitionError, ValueError) as e:
            return Response({"error": str(e)}, status=400)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

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
            result = InitiatePaymentService.execute(payable_type='order', payable_id=pk, provider=provider, phone=phone, bank_code=bank_code)
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
        category = request.data.get('category', 'other')

        order = self.get_object()
        if order.customer != request.user:
            return Response({"error": "Only the customer can dispute this order"}, status=403)

        from django.db import IntegrityError
        from domain.fsm.order_fsm import InvalidStateTransitionError
        from domain.services.admin_services import DisputeOrderService
        try:
            dispute = DisputeOrderService.execute(order.id, request.user, reason, category=category)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)
        except InvalidStateTransitionError as e:
            return Response({"error": str(e)}, status=400)
        except IntegrityError:
            return Response({"error": "An active dispute already exists for this order"}, status=409)

        return Response(DisputeSerializer(dispute).data)

    @action(detail=True, methods=['POST'])
    def refund(self, request, pk=None):
        order = self.get_object()
        if order.customer != request.user:
            return Response({"error": "Only the customer can request a refund"}, status=403)
            
        from domain.services.payment_services import RefundPaymentService
        try:
            success = RefundPaymentService.execute('order', order.id)
            if success:
                return Response({"status": "Refund initiated successfully"})
            else:
                return Response({"error": "Refund initiation failed"}, status=400)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['GET'], url_path='batch-details')
    def batch_details(self, request, pk=None):
        """Get all orders in the same batch as this order"""
        order = self.get_object()
        if not order.batch:
            return Response({'error': 'No batch'}, status=404)
        
        batch_orders = order.batch.orders.select_related(
            'restaurant', 'restaurant__owner', 'customer', 'driver', 'driver__user'
        ).prefetch_related('items', 'items__food').all()
        
        return Response(OrderSerializer(batch_orders, many=True).data)

class DisputeViewSet(viewsets.ModelViewSet):
    queryset = Dispute.objects.all()
    serializer_class = DisputeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Dispute.objects.all()
        if user.role == 'customer':
            qs = qs.filter(raised_by=user)
        elif user.role != 'admin':
            return Dispute.objects.none()

        status = self.request.query_params.get('status')
        if status:
            qs = qs.filter(status=status)
        return qs.select_related('order', 'raised_by', 'assigned_to', 'resolved_by')

    @action(detail=True, methods=['POST'])
    def resolve(self, request, pk=None):
        if request.user.role != 'admin':
            return Response({"error": "Only admins can resolve disputes"}, status=403)

        from domain.services.admin_services import DisputeResolutionService

        resolution = request.data.get('resolution')
        if resolution not in ('RESOLVED_REFUND', 'RESOLVED_PARTIAL', 'RESOLVED_DISMISSED'):
            return Response({"error": "resolution must be one of: RESOLVED_REFUND, RESOLVED_PARTIAL, RESOLVED_DISMISSED"}, status=400)

        refund_amount = request.data.get('refund_amount')
        if refund_amount is not None:
            try:
                from decimal import Decimal
                refund_amount = Decimal(str(refund_amount))
            except Exception:
                return Response({"error": "invalid refund_amount"}, status=400)

        try:
            dispute = DisputeResolutionService.resolve(
                dispute_id=pk,
                actor=request.user,
                resolution=resolution,
                resolution_note=request.data.get('resolution_note', ''),
                refund_amount=refund_amount,
            )
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

        return Response(DisputeSerializer(dispute).data)

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

class UniversalSearchView(APIView):
    """
    Search across multiple models (Restaurants, Foods).
    Uses Postgres TrigramSimilarity for fuzzy matching.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        query = request.query_params.get('q', '')
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')

        if not query or len(query) < 2:
            return Response({
                "restaurants": [],
                "products": []
            })

        # Search Restaurants — Prefetch foods to avoid N+1 in serializer
        restaurants_qs = Restaurant.objects.filter(is_active=True).prefetch_related('sections__foods').annotate(
            similarity=TrigramSimilarity('name', query)
        ).filter(similarity__gt=0.1).order_by('-similarity')[:15]

        # Search Products — select_related for vendor info
        products_qs = Food.objects.select_related('section__restaurant').filter(is_available=True).annotate(
            similarity=TrigramSimilarity('name', query)
        ).filter(similarity__gt=0.1).order_by('-similarity')[:30]

        # Search Marketplace
        marketplace_qs = MarketplaceProduct.objects.select_related('seller', 'category').prefetch_related('images').filter(
            status='ACTIVE'
        ).annotate(
            similarity=TrigramSimilarity('name', query)
        ).filter(similarity__gt=0.1).order_by('-similarity')[:20]

        # Collect all required vendor data for the products found
        vendor_ids = set(p.vendor_id for p in products_qs)
        # Also include restaurants that matched the name search
        vendor_ids.update(r.id for r in restaurants_qs)
        
        # Prefetch all vendors to serve as a lookup table
        all_vendors = Restaurant.objects.filter(id__in=vendor_ids).prefetch_related('sections__foods')
        
        return Response({
            "restaurants": RestaurantListSerializer(restaurants_qs, many=True, context={'request': request}).data,
            "products": FoodSerializer(products_qs, many=True, context={'request': request}).data,
            "marketplace": MarketplaceProductSerializer(marketplace_qs, many=True, context={'request': request}).data,
            "vendors": RestaurantListSerializer(all_vendors, many=True, context={'request': request}).data
        })


class AppConfigView(APIView):
    """Public endpoint for app version config and update URLs."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({
            "minimum_version": "1.0.1",
            "latest_version": "1.0.1",
            "update_url": "https://play.google.com/store/apps/details?id=com.mwanachuoshop.app",
        })


