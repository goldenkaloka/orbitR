from decimal import Decimal

from api.serializers.core import (
    AdminRestaurantSerializer,
    DriverSerializer,
    OrderSerializer,
    RestaurantSerializer,
    UserSerializer,
)
from apps.delivery.models import Driver
from apps.orders.models import Order
from apps.restaurants.models import Restaurant
from apps.users.models import User
from apps.wallets.models import Wallet
from django.core.cache import cache
from django.db.models import Count, Exists, OuterRef, Q, Subquery, Sum
from django.utils import timezone
from domain.fsm.order_fsm import InvalidStateTransitionError
from domain.services.admin_services import (
    DisputeOrderService,
    PromoteUserService,
    UpdateDriverService,
)
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response


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
        """Platform-wide statistics with caching.

        ``total_revenue`` / ``today_revenue`` are the platform's **net** cut,
        not gross merchandise value: the ledger already nets out the pickup
        payout fee, and the gateway fees paid on wallet cashouts are subtracted
        here. ``total_gmv`` keeps the old "everything customers spent" figure so
        the two are never confused again.
        """
        cache_key = "admin_platform_stats"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        total_users = User.objects.count()
        total_orders = Order.objects.count()
        today_orders = Order.objects.filter(created_at__gte=today_start).count()

        from apps.payments.models import LedgerEntry, PickupDisbursement
        from apps.wallets.models import WalletPayout, WalletTransaction

        def _sum(queryset, field):
            return Decimal(str(queryset.aggregate(Sum(field))[f'{field}__sum'] or 0))

        gmv = Order.objects.filter(status='DELIVERED')
        today_gmv = gmv.filter(created_at__gte=today_start)

        # platform_revenue rows already have the pickup payout fee deducted.
        revenue_entries = LedgerEntry.objects.filter(entry_type='platform_revenue')
        platform_revenue = _sum(revenue_entries, 'amount')
        today_platform_revenue = _sum(
            revenue_entries.filter(created_at__gte=today_start), 'amount',
        )

        # The 0.5% cashout fee is collected as a wallet transaction, not a
        # ledger row, so it has to be added back explicitly: netting the
        # gateway cost without its matching income understates revenue.
        fee_income_entries = WalletTransaction.objects.filter(
            tx_type='CREDIT', transaction_type='FEE', status='POSTED',
        )
        cashout_fee_income = _sum(fee_income_entries, 'amount')
        today_cashout_fee_income = _sum(
            fee_income_entries.filter(created_at__gte=today_start), 'amount',
        )

        # What the gateway kept for moving the money. A payout still in
        # PROCESSING/UNKNOWN may have moved already, so count it.
        cashout_payouts = WalletPayout.objects.filter(
            status__in=('SUCCESS', 'PROCESSING', 'UNKNOWN')
        )
        cashout_fees = _sum(cashout_payouts, 'gateway_fee')
        today_cashout_fees = _sum(
            cashout_payouts.filter(created_at__gte=today_start), 'gateway_fee',
        )

        pickup_disbursements = PickupDisbursement.objects.filter(
            status=PickupDisbursement.STATUS_SUCCESS,
        )
        pickup_fees = _sum(pickup_disbursements, 'gateway_fee')
        today_pickup_fees = _sum(
            pickup_disbursements.filter(created_at__gte=today_start), 'gateway_fee',
        )

        total_revenue = platform_revenue + cashout_fee_income - cashout_fees
        today_revenue = (
            today_platform_revenue
            + today_cashout_fee_income
            - today_cashout_fees
        )

        active_drivers = Driver.objects.filter(status='ONLINE').count()
        total_drivers = Driver.objects.count()
        total_restaurants = Restaurant.objects.count()

        # Wallet balances are what the platform OWES users, not what it owns.
        # Keep `platform_balance` for existing dashboards but expose the two
        # distinct numbers so nobody reads liabilities as equity again.
        from domain.services.payment_services import get_platform_user
        platform_user = get_platform_user()
        wallet_liabilities = Wallet.objects.exclude(user=platform_user).aggregate(
            Sum('balance')
        )['balance__sum'] or 0
        platform_equity = Wallet.objects.filter(user=platform_user).aggregate(
            Sum('balance')
        )['balance__sum'] or 0

        data = {
            'total_users': total_users,
            'total_orders': total_orders,
            'today_orders': today_orders,
            # Net platform revenue — safe to show as "Revenue".
            'total_revenue': float(total_revenue),
            'today_revenue': float(today_revenue),
            # Gross merchandise value, kept for the old "everything spent" tile.
            'total_gmv': float(_sum(gmv, 'total_amount')),
            'today_gmv': float(_sum(today_gmv, 'total_amount')),
            # What the gateways charged to move the money.
            'total_gateway_fees': float(pickup_fees + cashout_fees),
            'today_gateway_fees': float(today_pickup_fees + today_cashout_fees),
            'pickup_gateway_fees': float(pickup_fees),
            'cashout_gateway_fees': float(cashout_fees),
            'cashout_fee_income': float(cashout_fee_income),
            'platform_revenue_gross': float(platform_revenue),
            'platform_balance': float(wallet_liabilities),
            'wallet_liabilities': float(wallet_liabilities),
            'platform_equity': float(platform_equity),
            'active_drivers': active_drivers,
            'total_drivers': total_drivers,
            'total_restaurants': total_restaurants,
        }
        cache.set(cache_key, data, 60) # Cache for 60 seconds
        return Response(data)

    @action(detail=False, methods=['GET'])
    def users(self, request):
        """List all users with optional role filter."""
        role = request.query_params.get('role')
        from apps.restaurants.models import SectionStaff
        queryset = User.objects.annotate(
            has_driver_profile=Exists(Driver.objects.filter(user_id=OuterRef('pk'))),
            driver_profile_id=Subquery(Driver.objects.filter(user_id=OuterRef('pk')).values('id')[:1]),
            owns_restaurant=Exists(Restaurant.objects.filter(owner_id=OuterRef('pk'))),
            has_section_staff=Exists(SectionStaff.objects.filter(user_id=OuterRef('pk'))),
        ).order_by('-created_at')
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
            
        # paginate_queryset only returns None if pagination is misconfigured;
        # serialising the whole table here is unbounded, so hard-cap instead.
        return Response(UserSerializer(queryset[:100], many=True).data)

    @action(detail=False, methods=['GET'])
    def drivers(self, request):
        """List all drivers."""
        queryset = Driver.objects.select_related('user', 'home_zone').all()
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = DriverSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
            
        return Response(DriverSerializer(queryset[:100], many=True).data)

    @action(detail=False, methods=['GET'])
    def restaurants(self, request):
        """List all restaurants (admin view, includes owner identity)."""
        from apps.restaurants.utils import compute_busy_restaurant_ids
        queryset = (
            Restaurant.objects
            .select_related('owner')
            .prefetch_related('sections', 'sections__foods')
            .annotate(
                active_order_count=Count(
                    'orders',
                    filter=Q(orders__is_paid=True)
                    & ~Q(orders__status__in=[
                        'DELIVERED', 'CANCELLED', 'EXPIRED', 'REFUNDED',
                    ]),
                )
            )
            .all()
            .order_by('-created_at')
        )
        page = self.paginate_queryset(queryset)
        targets = page if page is not None else list(queryset[:100])
        busy_ids = set(compute_busy_restaurant_ids([r.id for r in targets]))
        serializer = AdminRestaurantSerializer(
            targets, many=True, context={'request': request, 'busy_ids': busy_ids}
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
            
        return Response(serializer.data)

    @action(detail=False, methods=['POST'], url_path='create-restaurant')
    def create_restaurant(self, request):
        """
        Create a restaurant owned by the acting admin.

        Lets an admin stand up an ops-owned restaurant directly - no customer
        needs to be promoted to vendor first. An explicit ?owner_id (admin or
        vendor) may be supplied to hand ownership elsewhere at creation time.
        Body: { name|restaurant_name, ?location, ?category, ?description,
                ?latitude, ?longitude, ?dispatch_mode, ?source_type, ?owner_id },
                plus optional multipart image/logo files.
        """
        from domain.services.admin_services import ManageRestaurantService

        name = request.data.get('name') or request.data.get('restaurant_name')
        if not name:
            return Response(
                {'error': 'name is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        owner = request.user
        owner_id = request.data.get('owner_id')
        if owner_id:
            owner = User.objects.filter(id=owner_id).first()
            if owner is None:
                return Response(
                    {'error': 'Owner user not found'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            if owner.role not in ('admin', 'vendor'):
                return Response(
                    {'error': 'Owner must have role admin or vendor'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            restaurant = ManageRestaurantService.create_restaurant(
                owner=owner,
                name=name,
                location=request.data.get('location', ''),
                description=request.data.get('description', ''),
                category=request.data.get('category', 'General'),
                latitude=request.data.get('latitude'),
                longitude=request.data.get('longitude'),
                image=request.FILES.get('image'),
                logo=request.FILES.get('logo'),
                dispatch_mode=request.data.get('dispatch_mode'),
                source_type=request.data.get('source_type'),
                public_disclaimer=request.data.get('public_disclaimer', ''),
                settlement_mode=request.data.get('settlement_mode'),
                payout_phone=request.data.get('payout_phone', ''),
                payout_network=request.data.get('payout_network', ''),
                payout_account_name=request.data.get('payout_account_name', ''),
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        from api.views.core import invalidate_restaurant_cache
        invalidate_restaurant_cache(restaurant)

        return Response({
            'message': f'{restaurant.name} created',
            'restaurant': AdminRestaurantSerializer(restaurant).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['GET'], url_path='my-restaurants')
    def my_restaurants(self, request):
        """
        Restaurants owned by the requesting admin.

        Powers the admin "Restaurants" tab: an admin operates the restaurants
        they own with the same order controls a vendor has. Ownership is a
        plain Restaurant.owner FK, so an admin can own many.
        """
        from apps.restaurants.utils import compute_busy_restaurant_ids
        queryset = (
            Restaurant.objects
            .select_related('owner')
            .prefetch_related('sections', 'sections__foods')
            .filter(owner=request.user)
            .annotate(
                active_order_count=Count(
                    'orders',
                    filter=Q(orders__is_paid=True)
                    & ~Q(orders__status__in=[
                        'DELIVERED', 'CANCELLED', 'EXPIRED', 'REFUNDED',
                    ]),
                )
            )
            .order_by('-active_order_count', '-created_at')
        )
        page = self.paginate_queryset(queryset)
        targets = page if page is not None else list(queryset)
        busy_ids = set(compute_busy_restaurant_ids([r.id for r in targets]))
        serializer = AdminRestaurantSerializer(
            targets, many=True, context={'request': request, 'busy_ids': busy_ids}
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(
        detail=False,
        methods=['GET'],
        url_path='restaurants/(?P<restaurant_id>[^/.]+)/orders',
    )
    def restaurant_orders(self, request, restaurant_id=None):
        """
        Incoming orders for a single restaurant, for the admin-run console.

        Mirrors the vendor order feed but scoped explicitly to one restaurant
        so the reused vendor order controls act on the right kitchen.
        Optional ?status= and ?active_only=1 filters.
        """
        if not Restaurant.objects.filter(id=restaurant_id).exists():
            return Response({'error': 'Restaurant not found'}, status=status.HTTP_404_NOT_FOUND)

        queryset = Order.objects.select_related(
            'customer',
            'restaurant',
            'restaurant__owner',
            'driver',
            'driver__user',
            'batch',
            'payment',
        ).prefetch_related(
            'items',
            'items__food',
            'items__section',
            'batch__orders',
            'batch__section_statuses',
            'pool__members',
            'pool__members__user',
            'reviews',
        ).filter(restaurant_id=restaurant_id, is_paid=True).order_by('-created_at')

        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        if request.query_params.get('active_only') in ('1', 'true', 'True'):
            queryset = queryset.exclude(
                status__in=['DELIVERED', 'CANCELLED', 'EXPIRED', 'REFUNDED']
            )

        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(OrderSerializer(page, many=True).data)
        return Response(OrderSerializer(queryset[:100], many=True).data)

    @action(detail=False, methods=['GET'])
    def orders(self, request):
        """List all orders with optional status filter."""
        status_filter = request.query_params.get('status')
        queryset = Order.objects.select_related(
            'customer',
            'restaurant',
            'restaurant__owner',
            'driver',
            'driver__user',
            'batch',
            'payment',
        ).prefetch_related(
            'items',
            'items__food',
            'items__section',
            'batch__orders',
            'batch__section_statuses',
            'pool__members',
            'pool__members__user',
            'reviews',
        ).all().order_by('-created_at')
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = OrderSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)
            
        return Response(OrderSerializer(queryset[:100], many=True).data)

    @action(detail=False, methods=['GET'], url_path='concierge-orders')
    def concierge_orders(self, request):
        """Orders waiting for an ops availability call, plus confirmed unpaid ones."""
        queryset = Order.objects.select_related(
            'customer', 'restaurant', 'restaurant__owner', 'driver', 'batch',
        ).prefetch_related('items', 'items__food', 'items__section').filter(
            Q(restaurant__source_type=Restaurant.SOURCE_PLATFORM_MANAGED)
            | Q(restaurant__settlement_mode=Restaurant.SETTLEMENT_PLATFORM_PREPAID),
            status__in=[
                'AWAITING_CONFIRMATION', 'CONFIRMED', 'PREPARING', 'READY', 'ASSIGNED',
            ],
        ).exclude(
            confirmation_status=Order.CONFIRMATION_UNAVAILABLE,
        ).filter(
            Q(confirmation_status=Order.CONFIRMATION_PENDING)
            | Q(restaurant_payment_status=Order.RESTAURANT_UNPAID)
        ).order_by('-created_at')
        return Response(OrderSerializer(queryset[:100], many=True).data)

    @action(detail=False, methods=['POST'], url_path='concierge-confirm')
    def concierge_confirm(self, request):
        from domain.services.orders.concierge import ConciergeOrderService

        order_id = request.data.get('order_id')
        if not order_id:
            return Response({'error': 'order_id is required'}, status=400)
        try:
            order = ConciergeOrderService.confirm(
                order_id,
                request.user,
                confirmed_total=request.data.get('confirmed_total'),
                note=request.data.get('note', ''),
            )
        except (ValueError, PermissionError) as exc:
            return Response({'error': str(exc)}, status=400)
        return Response(OrderSerializer(order).data)

    @action(detail=False, methods=['POST'], url_path='concierge-unavailable')
    def concierge_unavailable(self, request):
        from domain.services.orders.concierge import ConciergeOrderService

        order_id = request.data.get('order_id')
        if not order_id:
            return Response({'error': 'order_id is required'}, status=400)
        try:
            order = ConciergeOrderService.mark_unavailable(
                order_id,
                request.user,
                reason=request.data.get('reason', 'Restaurant could not fulfil the order.'),
            )
        except (ValueError, PermissionError) as exc:
            return Response({'error': str(exc)}, status=400)
        return Response(OrderSerializer(order).data)

    @action(detail=False, methods=['POST'], url_path='concierge-pay')
    def concierge_pay(self, request):
        from domain.services.orders.concierge import ConciergeOrderService

        order_id = request.data.get('order_id')
        if not order_id:
            return Response({'error': 'order_id is required'}, status=400)
        try:
            ConciergeOrderService.pay_restaurant(
                order_id,
                request.user,
                account_number=request.data.get('account_number'),
                network=request.data.get('network'),
            )
        except (ValueError, PermissionError) as exc:
            return Response({'error': str(exc)}, status=400)
        order = Order.objects.get(id=order_id)
        return Response(OrderSerializer(order).data)

    @action(detail=False, methods=['POST'], url_path='promote-vendor')
    def promote_vendor(self, request):
        """
        Promote a registered user to vendor role and create their restaurant.
        Body: { user_id, restaurant_name, location, ?category, ?description,
                ?dispatch_mode }
        dispatch_mode decides who allocates drivers for this restaurant's
        batches. Defaults to 'admin'.
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
                image=request.FILES.get('image'),
                logo=request.FILES.get('logo'),
                dispatch_mode=request.data.get('dispatch_mode'),
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        from api.views.core import invalidate_restaurant_cache
        invalidate_restaurant_cache(restaurant)

        return Response({
            'message': f'{user.username} promoted to vendor',
            'user': UserSerializer(user).data,
            'restaurant': RestaurantSerializer(restaurant).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['POST'], url_path='promote-driver')
    def promote_driver(self, request):
        """
        Promote a registered user to platform driver.
        Body: { user_id, ?can_assign, ?priority, ?home_restaurant_id, ?home_zone_id, ?max_active_orders, ?vehicle_type }
        Admin-promoted drivers are always platform drivers (`driver_type='platform'`).
        """
        user_id = request.data.get('user_id')
        if not user_id:
            return Response({'error': 'user_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        extra = {}
        for key in ('can_assign', 'priority', 'home_restaurant_id', 'home_zone_id', 'max_active_orders', 'vehicle_type'):
            if key in request.data:
                extra[key] = request.data[key]

        try:
            user, driver = PromoteUserService.to_driver(user_id=user_id, **extra)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except User.DoesNotExist:
            return Response({'error': 'User not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'message': f'{user.username} promoted to permanent driver',
            'user': UserSerializer(user).data,
            'driver': DriverSerializer(driver).data,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['POST'], url_path='update-driver')
    def update_driver(self, request):
        """
        Update driver properties.
        Body: { driver_id, ?driver_type, ?can_assign, ?priority, ?status, ?home_zone_id, ?max_active_orders }
        """
        driver_id = request.data.get('driver_id')
        if not driver_id:
            return Response({'error': 'driver_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        fields = {}
        for key in ('driver_type', 'can_assign', 'priority', 'status', 'home_zone_id',
                     'max_active_orders', 'current_active_orders', 'max_radius_km'):
            if key in request.data:
                fields[key] = request.data[key]

        try:
            driver = UpdateDriverService.execute(driver_id=driver_id, **fields)
        except Driver.DoesNotExist:
            return Response({'error': 'Driver not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'message': f'Driver updated',
            'driver': DriverSerializer(driver).data,
        })

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
        Body: { restaurant_id, name, location, latitude, longitude, description,
                category, source_type, is_verified_partner, public_disclaimer,
                dispatch_mode }
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
            'source_type': request.data.get('source_type'),
            'is_verified_partner': request.data.get('is_verified_partner'),
            'public_disclaimer': request.data.get('public_disclaimer'),
            'dispatch_mode': request.data.get('dispatch_mode'),
            'settlement_mode': request.data.get('settlement_mode'),
            'payout_phone': request.data.get('payout_phone'),
            'payout_network': request.data.get('payout_network'),
            'payout_account_name': request.data.get('payout_account_name'),
            'owner_id': request.data.get('owner_id'),
            'image': request.FILES.get('image'),
            'logo': request.FILES.get('logo')
        }
        
        try:
            restaurant = ManageRestaurantService.update_restaurant(restaurant_id, **update_data)
            from api.views.core import invalidate_restaurant_cache
            invalidate_restaurant_cache(restaurant)
            return Response({
                'message': f'Restaurant {restaurant.name} updated',
                'restaurant': RestaurantSerializer(restaurant).data,
            })
        except Restaurant.DoesNotExist:
            return Response({'error': 'Restaurant not found'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

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

    @action(detail=False, methods=['GET'], url_path='pending-batches')
    def pending_batches(self, request):
        """
        Dispatch queue: batches whose food is ready and that still need a
        driver allocated.

        Includes both admin-dispatch and restaurant-dispatch batches: an admin
        may allocate in either mode as an escalation, so both belong in the
        queue. Each row carries dispatch_mode so the UI can show who is
        expected to act.

        This replaces the old pending_manual_assignment endpoint, which
        filtered batch__isnull=True and therefore always returned nothing —
        every delivered order is swept into a DeliveryBatch on confirmation.
        """
        from apps.orders.models import DeliveryBatch
        from domain.services.dispatch_routing import resolve_batch_dispatch_mode

        batches = (
            DeliveryBatch.objects
            .filter(status__in=['PENDING_DRIVER', 'PRE_ASSIGNING'])
            .select_related('restaurant')
            .prefetch_related(
                'orders__customer',
                'orders__restaurant',
                'batch_restaurants__restaurant',
            )
            .order_by('waiting_since', 'created_at')
        )

        now = timezone.now()
        payload = []
        for batch in batches:
            orders = list(batch.orders.all())
            waiting_since = batch.waiting_since or batch.created_at
            waiting_minutes = max(0, int((now - waiting_since).total_seconds() // 60))
            restaurant_names = sorted({o.restaurant.name for o in orders if o.restaurant})
            dispatch_mode = resolve_batch_dispatch_mode(batch)

            # Pooled batches carry their restaurants in pickup order so the
            # admin can sanity-check the run before allocating a driver.
            pickup_sequence = sorted(
                list(batch.batch_restaurants.all()),
                key=lambda br: br.pickup_sequence,
            )

            payload.append({
                'id': str(batch.id),
                'short_id': str(batch.id)[:8].upper(),
                'status': batch.status,
                'batch_type': batch.batch_type,
                'restaurant_count': len(pickup_sequence) or 1,
                'restaurants': [
                    {
                        'id': str(br.restaurant_id),
                        'name': br.restaurant.name,
                        'pickup_sequence': br.pickup_sequence,
                    }
                    for br in pickup_sequence
                ],
                'restaurant_id': str(batch.restaurant_id) if batch.restaurant_id else None,
                'restaurant_name': (
                    restaurant_names[0] if len(restaurant_names) == 1
                    else f"{len(restaurant_names)} restoranti"
                ),
                'dispatch_mode': dispatch_mode,
                'order_count': len(orders),
                'is_inter_zone': batch.is_inter_zone,
                'waiting_minutes': waiting_minutes,
                'waiting_since': waiting_since,
                'zone_ids': batch.zone_ids,
                'total_payout': float(batch.driver_payout) if batch.driver_payout else None,
                'orders': [
                    {
                        'id': str(o.id),
                        'short_id': str(o.id)[:8].upper(),
                        'status': o.status,
                        'customer': o.customer.username,
                        'delivery_address': o.delivery_address,
                        'total_amount': float(o.total_amount),
                    }
                    for o in orders
                ],
            })

        return Response({'count': len(payload), 'batches': payload})

    @action(detail=False, methods=['GET'], url_path='batches/(?P<batch_id>[^/.]+)/eligible-drivers')
    def eligible_drivers(self, request, batch_id=None):
        """
        Drivers an admin can allocate to this batch, best first.

        Ranked by capacity headroom then rating so the top of the list is the
        driver least likely to be overloaded.
        """
        from apps.delivery.models import DeliveryAssignment
        from apps.orders.models import DeliveryBatch
        from domain.services.permissions import restaurant_visible_drivers

        try:
            batch = DeliveryBatch.objects.get(id=batch_id)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=status.HTTP_404_NOT_FOUND)

        # A pooled batch spans several restaurants, so the eligible set is the
        # union of every restaurant's drivers. Restaurant-affiliated drivers are
        # filtered out below because they can only serve their own kitchen —
        # AssignBatchService rejects them for a multi-restaurant run.
        restaurant_ids = set(batch.orders.values_list('restaurant_id', flat=True))
        if batch.restaurant_id:
            restaurant_ids.add(batch.restaurant_id)
        restaurant_ids.discard(None)

        if not restaurant_ids:
            return Response({'error': 'Batch has no orders'}, status=status.HTTP_400_BAD_REQUEST)

        restaurants = list(Restaurant.objects.filter(id__in=restaurant_ids))
        if not restaurants:
            return Response({'error': 'Batch has no restaurants'}, status=status.HTTP_400_BAD_REQUEST)

        by_id = {}
        for restaurant in restaurants:
            for driver in restaurant_visible_drivers(restaurant):
                by_id.setdefault(driver.id, driver)
        candidates = list(by_id.values())

        is_pooled = len(restaurants) > 1

        # Drivers already holding an open assignment on this batch are excluded.
        busy_ids = set(
            DeliveryAssignment.objects
            .filter(
                driver__in=candidates,
                delivered_at__isnull=True,
                order__in=batch.orders.all(),
            )
            .values_list('driver_id', flat=True)
        )

        rows = []
        for driver in candidates:
            load = driver.current_active_orders
            if driver.id in busy_ids:
                load += 1
            # Only platform drivers can cover every restaurant in a pooled run.
            can_serve_all = (
                driver.driver_type == 'platform'
                or driver.home_restaurant_id in restaurant_ids
            ) and (driver.driver_type == 'platform' or not is_pooled)
            rows.append({
                'id': str(driver.id),
                'name': driver.user.username,
                'driver_type': driver.driver_type,
                'vehicle_type': driver.vehicle_type,
                'status': driver.status,
                'rating': float(driver.rating),
                'priority': driver.priority,
                'current_active_orders': load,
                'max_active_orders': driver.max_active_orders,
                'capacity_headroom': driver.max_active_orders - load,
                'home_restaurant_id': (
                    str(driver.home_restaurant_id) if driver.home_restaurant_id else None
                ),
                'can_serve': can_serve_all,
            })

        rows.sort(key=lambda r: (not r['can_serve'], -r['capacity_headroom'], -r['priority'], -r['rating']))

        return Response({
            'batch_id': str(batch.id),
            'restaurant_ids': [str(r.id) for r in restaurants],
            'restaurant_count': len(restaurants),
            'is_multi_restaurant': is_pooled,
            'count': len(rows),
            'drivers': rows,
        })

    @action(detail=False, methods=['POST'], url_path='manual-assign-driver')
    def manual_assign_driver(self, request):
        """
        Allocate a driver to a batch (preferred) or a single order.

        Delegates to AssignBatchService / AssignDriverService so the admin
        path gets the same guarantees as every other allocation: FSM
        transition, DeliveryAssignment audit row, DriverAssigned event,
        Centrifugo broadcast, restaurant-eligibility and capacity checks.

        Body: { batch_id | order_id, driver_id }
        """
        from apps.orders.models import DeliveryBatch
        from domain.services.orders.order_assignment import (
            AssignBatchService,
            AssignDriverService,
        )

        batch_id = request.data.get('batch_id')
        order_id = request.data.get('order_id')
        driver_id = request.data.get('driver_id')

        if not driver_id or not (batch_id or order_id):
            return Response(
                {'error': 'driver_id and one of batch_id or order_id are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if batch_id:
            unpaid_prepaid = Order.objects.filter(
                batch_id=batch_id,
                restaurant__settlement_mode=Restaurant.SETTLEMENT_PLATFORM_PREPAID,
            ).exclude(restaurant_payment_status=Order.RESTAURANT_PAID)
            if unpaid_prepaid.exists():
                return Response(
                    {'error': 'Pay the restaurant before assigning a driver for this batch.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                AssignBatchService.execute(str(batch_id), str(driver_id), request.user)
            except DeliveryBatch.DoesNotExist:
                return Response({'error': 'Batch not found'}, status=status.HTTP_404_NOT_FOUND)
            except ValueError as e:
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            except PermissionError as e:
                return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
            except Driver.DoesNotExist:
                return Response({'error': 'Driver not found'}, status=status.HTTP_404_NOT_FOUND)

            orders = DeliveryBatch.objects.get(id=batch_id).orders.all()
            return Response({
                'message': 'Driver allocated to batch',
                'batch_id': str(batch_id),
                'orders': OrderSerializer(orders, many=True).data,
            })

        target = Order.objects.filter(id=order_id).first()
        if (
            target
            and target.restaurant
            and target.restaurant.settlement_mode == Restaurant.SETTLEMENT_PLATFORM_PREPAID
            and target.restaurant_payment_status != Order.RESTAURANT_PAID
        ):
            return Response(
                {'error': 'Pay the restaurant before assigning a driver to this order.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            order = AssignDriverService.execute(str(order_id), str(driver_id), request.user)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except PermissionError as e:
            return Response({'error': str(e)}, status=status.HTTP_403_FORBIDDEN)
        except Driver.DoesNotExist:
            return Response({'error': 'Driver not found'}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            'message': 'Driver allocated to order',
            'order': OrderSerializer(order).data,
        })

    @action(detail=False, methods=['POST'], url_path='wallet-adjust')
    def wallet_adjust(self, request):
        """Credit or debit a wallet with an auditable reason.

        This is the compliant way to issue goodwill credits, correct a
        mistaken payout, or manually settle a debt — every movement is a
        ledger row with the acting admin's reason attached.
        """
        from decimal import Decimal, InvalidOperation

        from domain.services.wallet_services import InsufficientFunds, WalletDomainService
        direction = str(request.data.get('direction') or '').upper()
        amount_raw = request.data.get('amount')
        reason = str(request.data.get('reason') or '').strip()
        wallet_id = request.data.get('wallet_id')
        user_id = request.data.get('user_id')
        reference = request.data.get('reference')
        idempotency_key = (
            request.headers.get('Idempotency-Key')
            or request.data.get('idempotency_key')
        )

        if direction not in ('CREDIT', 'DEBIT'):
            return Response({'error': 'direction must be CREDIT or DEBIT'}, status=400)
        if not reason:
            return Response({'error': 'reason is required'}, status=400)
        try:
            amount = Decimal(str(amount_raw))
        except (InvalidOperation, TypeError, ValueError):
            return Response({'error': 'invalid amount'}, status=400)
        if amount <= 0:
            return Response({'error': 'amount must be greater than zero'}, status=400)

        if wallet_id:
            wallet = Wallet.objects.filter(id=wallet_id).first()
        elif user_id:
            wallet, _ = Wallet.objects.get_or_create(user_id=user_id)
        else:
            return Response({'error': 'wallet_id or user_id is required'}, status=400)
        if wallet is None:
            return Response({'error': 'Wallet not found'}, status=404)

        admin_reference = f"admin:{reference}" if reference else None
        try:
            wallet, txn, created = WalletDomainService.post_transaction(
                wallet=wallet,
                amount=amount,
                tx_type=direction,
                transaction_type='ADJUSTMENT',
                description=f"Admin adjustment: {reason}",
                reference=admin_reference,
                idempotency_key=str(idempotency_key)[:128] if idempotency_key else None,
            )
        except InsufficientFunds as exc:
            return Response({'error': str(exc)}, status=400)
        except ValueError as exc:
            return Response({'error': str(exc)}, status=400)

        return Response({
            'wallet_id': str(wallet.id),
            'balance': f"{wallet.balance:.2f}",
            'transaction_id': str(txn.id),
            'duplicate': not created,
            'direction': direction,
            'amount': f"{txn.amount:.2f}",
        })

    @action(detail=False, methods=['GET'], url_path='wallet-payouts')
    def wallet_payouts(self, request):
        from apps.wallets.models import WalletPayout

        status_filter = request.query_params.get('status')
        qs = WalletPayout.objects.select_related('actor').order_by('-created_at')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response([
            {
                'id': str(p.id),
                'user_id': str(p.actor_id) if p.actor_id else None,
                'user_phone': p.actor.phone if p.actor else None,
                'status': p.status,
                'amount': float(p.amount),
                'fee': float(p.fee),
                'net_amount': float(p.net_amount),
                'provider': p.provider,
                'destination_phone': p.destination_phone,
                'reference': p.reference,
                'provider_reference': p.provider_reference,
                'failure_reason': p.failure_reason,
                'created_at': p.created_at,
                'completed_at': p.completed_at,
            }
            for p in qs[:200]
        ])

    @action(detail=False, methods=['POST'], url_path='reconcile-payout')
    def reconcile_payout(self, request):
        from apps.wallets.models import WalletPayout
        from domain.services.wallet_payout_services import reconcile_payout

        payout_id = request.data.get('payout_id')
        if not payout_id:
            return Response({'error': 'payout_id is required'}, status=400)
        if not WalletPayout.objects.filter(id=payout_id).exists():
            return Response({'error': 'Payout not found'}, status=404)
        payout = reconcile_payout(payout_id)
        return Response({
            'id': str(payout.id),
            'status': payout.status,
            'provider_reference': payout.provider_reference,
            'failure_reason': payout.failure_reason,
        })
