from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission

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
        permanent_drivers = Driver.objects.filter(driver_type='permanent').count()
        temp_drivers = Driver.objects.filter(driver_type='temporary').count()
        
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
            'permanent_drivers': permanent_drivers,
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
        """List all drivers with optional type filter."""
        driver_type = request.query_params.get('type')  # permanent / temporary
        queryset = Driver.objects.select_related('user').all()
        if driver_type:
            queryset = queryset.filter(driver_type=driver_type)
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
        Body: { order_id }
        """
        order_id = request.data.get('order_id')
        if not order_id:
            return Response({'error': 'order_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            order = DisputeOrderService.execute(order_id)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
        except InvalidStateTransitionError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({
            'message': f'Order {order_id} marked as DISPUTED',
            'order': OrderSerializer(order).data,
        })

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

    @action(detail=False, methods=['POST'], url_path='update-restaurant')
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

    @action(detail=False, methods=['POST'], url_path='delete-restaurant')
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
