from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission

from django.db import transaction
from django.db.models import Sum, Count, Q
from django.utils import timezone
from django.core.cache import cache

from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.orders.models import Order
from apps.delivery.models import Driver
from apps.wallets.models import Wallet

from api.serializers.core import UserSerializer, RestaurantSerializer, OrderSerializer, DriverSerializer
from domain.services.admin_services import PromoteUserService, DisputeOrderService
from domain.fsm.order_fsm import InvalidStateTransitionError


class IsAdmin(BasePermission):
    """Only allow users with role='admin'."""
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == 'admin'


class AdminViewSet(viewsets.GenericViewSet):
    """
    Admin-only endpoints for platform management.
    All actions require the requesting user to have role='admin'.
    """
    permission_classes = [IsAuthenticated, IsAdmin]
    
    def __init__(self, *args, **kwargs):
        print("DEBUG: AdminViewSet loaded with updated actions")
        super().__init__(*args, **kwargs)

    @action(detail=False, methods=['GET'])
    def stats(self, request):
        """Platform-wide statistics with caching."""
        cache_key = "admin_platform_stats"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        today = timezone.now().date()
        total_users = User.objects.count()
        total_orders = Order.objects.count()
        today_orders = Order.objects.filter(created_at__date=today).count()
        total_revenue = Order.objects.filter(
            status='DELIVERED'
        ).aggregate(Sum('total_amount'))['total_amount__sum'] or 0
        today_revenue = Order.objects.filter(
            status='DELIVERED', created_at__date=today
        ).aggregate(Sum('total_amount'))['total_amount__sum'] or 0
        active_drivers = Driver.objects.filter(status='ONLINE').count()
        total_drivers = Driver.objects.count()
        total_restaurants = Restaurant.objects.count()
        temp_drivers = Driver.objects.count()
        
        # Total funds currently in the system wallets
        platform_balance = Wallet.objects.aggregate(Sum('balance'))['balance__sum'] or 0

        data = {
            'total_users': total_users,
            'total_orders': total_orders,
            'today_orders': today_orders,
            'total_revenue': float(total_revenue),
            'today_revenue': float(today_revenue),
            'platform_balance': float(platform_balance),
            'active_drivers': active_drivers,
            'total_drivers': total_drivers,
            'temp_drivers': temp_drivers,
            'total_restaurants': total_restaurants,
        }
        cache.set(cache_key, data, 60) # Cache for 60 seconds
        return Response(data)

    @action(detail=False, methods=['GET'])
    def users(self, request):
        """List all users with optional role filter."""
        role = request.query_params.get('role')
        queryset = User.objects.all().order_by('-created_at')
        if role:
            queryset = queryset.filter(role=role)
        
        # Simple search by username or phone
        search = request.query_params.get('search')
        if search:
            queryset = queryset.filter(
                Q(username__icontains=search) |
                Q(phone__icontains=search)
            )
        
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = UserSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
            
        return Response(UserSerializer(queryset, many=True).data)

    @action(detail=False, methods=['GET'])
    def drivers(self, request):
        """List all drivers."""
        queryset = Driver.objects.select_related('user').all()
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = DriverSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
            
        return Response(DriverSerializer(queryset, many=True).data)

    @action(detail=False, methods=['GET'])
    def restaurants(self, request):
        """List all restaurants."""
        queryset = Restaurant.objects.select_related('owner').all()
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = RestaurantSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
            
        return Response(RestaurantSerializer(queryset, many=True).data)

    @action(detail=False, methods=['GET'])
    def orders(self, request):
        """List all orders with optional status filter."""
        status_filter = request.query_params.get('status')
        queryset = Order.objects.select_related('customer', 'restaurant', 'driver').all().order_by('-created_at')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = OrderSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
            
        return Response(OrderSerializer(queryset, many=True).data)

    @action(detail=False, methods=['POST'], url_path='promote-vendor')
    def promote_vendor(self, request):
        """
        Promote a registered user to vendor role and create their restaurant.
        Body: { user_id, restaurant_name, location, ?category, ?description }
        """
        user_id = request.data.get('user_id')
        restaurant_name = request.data.get('restaurant_name')
        location = request.data.get('location', '')

        if not user_id or not restaurant_name:
            return Response(
                {'error': 'user_id and restaurant_name are required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            user, restaurant = PromoteUserService.to_vendor(
                user_id=user_id,
                restaurant_name=restaurant_name,
                location=location,
                description=request.data.get('description', ''),
                category=request.data.get('category', 'General'),
                latitude=request.data.get('latitude'),
                longitude=request.data.get('longitude'),
                image=request.FILES.get('image')
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'message': f'{user.username} promoted to vendor',
            'user': UserSerializer(user).data,
            'restaurant': RestaurantSerializer(restaurant).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['POST'], url_path='promote-driver')
    def promote_driver(self, request):
        """
        Promote a registered user to permanent driver.
        Body: { user_id }
        """
        user_id = request.data.get('user_id')
        if not user_id:
            return Response({'error': 'user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user, driver = PromoteUserService.to_driver(user_id=user_id)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'message': f'{user.username} promoted to permanent driver',
            'user': UserSerializer(user).data,
            'driver': DriverSerializer(driver).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['POST'], url_path='dispute-order')
    def dispute_order(self, request):
        """
        Admin marks an order as DISPUTED (e.g. restaurant mistake).
        Body: { order_id, reason, category }
        """
        order_id = request.data.get('order_id')
        if not order_id:
            return Response({'error': 'order_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        from django.db import IntegrityError

        try:
            reason = request.data.get('reason', 'Admin Dispute')
            category = request.data.get('category', 'other')
            dispute = DisputeOrderService.execute(order_id, request.user, reason, category=category)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidStateTransitionError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except IntegrityError:
            return Response({'error': 'An active dispute already exists for this order'}, status=status.HTTP_409_CONFLICT)

        from api.serializers.core import DisputeSerializer
        return Response(DisputeSerializer(dispute).data)

    @action(detail=False, methods=['POST'], url_path='toggle-restaurant')
    def toggle_restaurant(self, request):
        """
        Toggle a restaurant's is_active status.
        Body: { restaurant_id }
        """
        restaurant_id = request.data.get('restaurant_id')
        if not restaurant_id:
            return Response({'error': 'restaurant_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            from domain.services.admin_services import ManageRestaurantService
            restaurant = ManageRestaurantService.toggle_status(restaurant_id)
        except Restaurant.DoesNotExist:
            return Response({'error': 'Restaurant not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'message': f'Restaurant {restaurant.name} is now {"active" if restaurant.is_active else "inactive"}',
            'restaurant': RestaurantSerializer(restaurant).data,
        })

    @action(detail=False, methods=['POST'], url_path='update-restaurant', url_name='update-restaurant')
    def update_restaurant(self, request):
        """
        Update restaurant details.
        Body: { restaurant_id, name, location, latitude, longitude, description, category }
        """
        restaurant_id = request.data.get('restaurant_id')
        if not restaurant_id:
            return Response({'error': 'restaurant_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        from domain.services.admin_services import ManageRestaurantService
        update_data = {
            'name': request.data.get('name'),
            'location': request.data.get('location'),
            'latitude': request.data.get('latitude'),
            'longitude': request.data.get('longitude'),
            'description': request.data.get('description'),
            'category': request.data.get('category'),
            'image': request.FILES.get('image')
        }
        
        try:
            restaurant = ManageRestaurantService.update_restaurant(restaurant_id, **update_data)
            return Response({
                'message': f'Restaurant {restaurant.name} updated',
                'restaurant': RestaurantSerializer(restaurant).data,
            })
        except Restaurant.DoesNotExist:
            return Response({'error': 'Restaurant not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['POST'], url_path='delete-restaurant', url_name='delete-restaurant')
    def delete_restaurant(self, request):
        """
        Delete a restaurant.
        Body: { restaurant_id }
        """
        restaurant_id = request.data.get('restaurant_id')
        if not restaurant_id:
            return Response({'error': 'restaurant_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        from domain.services.admin_services import ManageRestaurantService
        try:
            ManageRestaurantService.delete_restaurant(restaurant_id)
            return Response({'message': 'Restaurant deleted successfully'})
        except Restaurant.DoesNotExist:
            return Response({'error': 'Restaurant not found'}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=False, methods=['GET'])
    def pending_manual_assignment(self, request):
        """Get READY orders that need driver assignment."""
        orders = Order.objects.filter(
            status='READY', batch__isnull=True
        ).select_related('restaurant', 'customer')
        return Response({
            'count': orders.count(),
            'orders': OrderSerializer(orders, many=True).data
        })

    @action(detail=False, methods=['POST'], url_path='manual-assign-driver')
    def manual_assign_driver(self, request):
        """
        Manually assign a driver to an order.
        Body: { order_id, driver_id }
        """
        order_id = request.data.get('order_id')
        driver_id = request.data.get('driver_id')

        if not order_id or not driver_id:
            return Response({'error': 'order_id and driver_id are required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)

        try:
            driver = Driver.objects.get(id=driver_id)
        except Driver.DoesNotExist:
            return Response({'error': 'Driver not found'}, status=status.HTTP_404_NOT_FOUND)

        from domain.fsm.driver_fsm import DriverFSM

        if order.status != 'READY':
            return Response({
                'error': f'Cannot assign driver to order in status {order.status}. Only READY orders can be manually assigned.'
            }, status=status.HTTP_400_BAD_REQUEST)

        if driver.status not in ['OFFLINE', 'AVAILABLE']:
            return Response({
                'error': f'Driver is in {driver.status} status. Driver must be OFFLINE or AVAILABLE.'
            }, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            from django.db.models import F
            from domain.fsm.driver_fsm import DriverFSM

            order.driver = driver
            order.status = 'ASSIGNED'
            order.save()

            # Driver goes directly to DELIVERING (ASSIGNED state removed from DriverFSM)
            if driver.status in ('OFFLINE', 'AVAILABLE'):
                new_status = DriverFSM.transition('AVAILABLE', 'DELIVERING')
            else:
                new_status = driver.status  # already DELIVERING

            Driver.objects.filter(id=driver.id).update(
                status=new_status,
                current_active_orders=F('current_active_orders') + 1
            )
            driver.refresh_from_db()

        return Response({
            'message': f'Driver {driver.user.username} assigned to order {order_id}',
            'order': OrderSerializer(order).data,
            'driver': DriverSerializer(driver).data
        })
