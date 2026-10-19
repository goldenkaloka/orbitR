import os
import logging

logger = logging.getLogger(__name__)

from api.serializers.core import (
    DisputeSerializer,
    DriverSerializer,
    FoodSerializer,
    NotificationSerializer,
    OrderSerializer,
    RestaurantListSerializer,
    RestaurantSerializer,
    UserSerializer,
)
from api.serializers.marketplace import MarketplaceProductSerializer
from apps.core.cache_keys import stable_cache_key
from apps.core.geo_utils import get_neighbors
from apps.core.models import Notification
from apps.delivery.models import Driver
from apps.marketplace.models import MarketplaceProduct
from apps.orders.models import DeliveryBatch, Dispute, Order
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.reviews.models import Review
from apps.users.models import User
from apps.wallets.models import Wallet, WalletTransaction
from django.conf import settings
from django.contrib.postgres.search import TrigramSimilarity
from django.core.cache import cache
from django.db import models
from django.db.models import Avg, Case, Count, DurationField, Exists, ExpressionWrapper, F, FloatField, OuterRef, Prefetch, Q, Subquery, Value, When
from django.db.models.functions import Cast, Coalesce, Extract
from django.utils import timezone
from domain.fsm.order_fsm import InvalidStateTransitionError
from domain.services.order_services import (
    AssignBatchService,
    AssignDriverService,
    CompletePickupService,
    ConfirmOrderService,
    DeliverOrderService,
    MarkReadyService,
    PickupOrderService,
    StartPreparingService,
)
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


def build_restaurant_mine_data(user, restaurant_id=None):
    from apps.restaurants.models import SectionStaff

    restaurant = None
    is_owner = False

    # A specific restaurant may be requested (admin console for an owned
    # restaurant, or a vendor with several). Only the owner or an admin may
    # address a restaurant by id this way.
    if restaurant_id:
        restaurant = (
            Restaurant.objects.select_related('owner')
            .prefetch_related('sections__foods')
            .filter(id=restaurant_id)
            .first()
        )
        if restaurant is not None:
            if getattr(user, 'role', None) == 'admin' or restaurant.owner_id == user.id:
                is_owner = True
            else:
                return {}

    if restaurant is None:
        restaurant = (
            Restaurant.objects.select_related('owner')
            .prefetch_related('sections__foods')
            .filter(owner=user)
            .order_by('created_at')
            .first()
        )
        is_owner = restaurant is not None
    if restaurant is None:
        staff_link = SectionStaff.objects.filter(user=user).select_related('section__restaurant').first()
        if not staff_link:
            return {}
        restaurant = staff_link.section.restaurant

    ctx = {'_user': user}
    if is_owner:
        data = RestaurantSerializer(restaurant, context=ctx).data
        data['staff_section_ids'] = []
    else:
        staff_section_ids = set(SectionStaff.objects.filter(user=user).values_list('section_id', flat=True))
        data = RestaurantSerializer(restaurant, context=ctx).data
        str_staff_section_ids = {str(sid) for sid in staff_section_ids}
        data['foods'] = [f for f in data.get('foods', []) if str(f.get('section')) in str_staff_section_ids or f.get('section') is None]
        data['staff_section_ids'] = list(str_staff_section_ids)

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

    # Bump restaurant list cache version so customers see updated availability
    try:
        cache.incr('restaurant_list_cache_version')
    except ValueError:
        cache.set('restaurant_list_cache_version', 1)

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

    # Drop scoped (admin console) restaurant snapshots for this restaurant.
    try:
        cache.delete_pattern(f"restaurant_mine_*_{restaurant.id}")
    except Exception:
        logger.debug("scoped restaurant cache invalidation skipped", exc_info=True)

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

def _invalidate_available_jobs_cache():
    """
    Drop every driver's cached available-jobs list.

    Keys are namespaced `available_jobs:*`, so a single pattern delete clears
    all of them. Claiming is rare relative to polling (a 15s TTL already bounds
    staleness), so the correctness win of every driver re-fetching outweighs
    the cost of re-running a handful of indexed queries.
    """
    try:
        cache.delete_pattern('available_jobs:*')
    except Exception:
        # Non-redis backends lack delete_pattern; the 15s TTL is the backstop.
        logger.debug("available_jobs cache invalidation skipped", exc_info=True)

def _release_payment_sync(order):
    """Release payment and credit wallets synchronously after delivery.

    Called from deliver and complete-pickup endpoints so wallet
    credit is immediate and does not depend on Celery. Idempotent via the
    DISBURSED guard inside ReleasePaymentService.
    """
    try:
        from apps.payments.models import Payment
        from domain.services.payment_services import ReleasePaymentService, record_failed_payout

        payment = Payment.objects.filter(
            payable_type='order', payable_id=str(order.id)
        ).first()
        if payment is None:
            logger.warning(f"No payment found for order {order.id}; skipping release.")
            return

        if payment.status == 'INITIATED':
            payment.status = 'COLLECTED'
            payment.save(update_fields=['status'])

        ReleasePaymentService.execute(payment.id)
    except Exception:
        logger.exception(f"Payment release failed for order {order.id}")
        if 'payment' in locals() and payment is not None:
            record_failed_payout(payment, f"release failed for order {order.id}")

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
        role = request.query_params.get('role')
        if len(query) < 3:
            return Response([])
        
        # Optimize for "billions": istartswith is faster than icontains on indexed fields
        # Also limit results and only return relevant fields if necessary
        filters = Q(username__icontains=query) | Q(phone__istartswith=query)
        if role:
            filters &= Q(role=role)
        from apps.restaurants.models import SectionStaff
        users = User.objects.filter(filters).annotate(
            has_driver_profile=Exists(Driver.objects.filter(user_id=OuterRef('pk'))),
            driver_profile_id=Subquery(Driver.objects.filter(user_id=OuterRef('pk')).values('id')[:1]),
            owns_restaurant=Exists(Restaurant.objects.filter(owner_id=OuterRef('pk'))),
            has_section_staff=Exists(SectionStaff.objects.filter(user_id=OuterRef('pk'))),
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
        if user.is_authenticated and user.serialized_role == 'admin':
            return queryset
        if user.is_authenticated and user.serialized_role in ('vendor', 'vendor_staff'):
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
        user = self.request.user
        food = serializer.instance
        from domain.services.permissions import check_restaurant_permission
        if not check_restaurant_permission(user, food.vendor):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied('Only the restaurant owner, staff, or admin can edit food items')
        serializer.save()
        invalidate_restaurant_cache(food.vendor)

class RestaurantViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = RestaurantSerializer
    public_actions = {'list', 'retrieve', 'delivery_fee'}

    def get_permissions(self):
        if getattr(self, 'action', None) in self.public_actions:
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_serializer_class(self):
        # Avoid heavy nested payloads on list endpoints.
        if getattr(self, "action", None) == "list":
            return RestaurantListSerializer
        return RestaurantSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if hasattr(self, '_list_busy_ids'):
            context['busy_ids'] = self._list_busy_ids
        context['user_location'] = getattr(self, '_user_location', None)
        return context

    def get_queryset(self):
        # Precompute delivery pricing per restaurant when the caller's location is
        # known, so the fee can be rendered in list UIs without extra round trips.
        self._user_location = None
        _lat = self.request.query_params.get('lat')
        _lng = self.request.query_params.get('lng')
        if _lat and _lng:
            try:
                from django.contrib.gis.geos import Point
                self._user_location = Point(float(_lng), float(_lat), srid=4326)
            except (TypeError, ValueError):
                self._user_location = None
        food_prefetch = Prefetch(
            'foods',
            queryset=Food.objects.only(
                'id', 'section_id', 'name', 'description', 'price',
                'category', 'is_available', 'image',
            ),
        )
        section_prefetch = Prefetch(
            'sections',
            queryset=RestaurantSection.objects.only(
                'id', 'restaurant_id', 'name', 'specialization', 'description',
                'image', 'is_active', 'sort_order', 'prep_time',
            ).prefetch_related(food_prefetch),
        )
        queryset = Restaurant.objects.select_related('owner').prefetch_related(section_prefetch).annotate(
            available_food_count=Count('sections__foods', filter=Q(sections__foods__is_available=True))
        )
        if getattr(self, "action", None) != "update_profile":
            queryset = queryset.filter(is_active=True)
        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category__iexact=category)

        # Sorting and scoring
        if getattr(self, "action", None) == "list":
            lat = self.request.query_params.get('lat')
            lng = self.request.query_params.get('lng')
            sort_by = self.request.query_params.get('sort_by', 'recommended')

            if lat and lng:
                from django.contrib.gis.db.models.functions import Distance
                from django.contrib.gis.geos import Point
                user_location = Point(float(lng), float(lat), srid=4326)
                queryset = queryset.annotate(
                    distance=Case(
                        When(geo_location__isnull=False, then=Distance('geo_location', user_location)),
                        default=Value(1e10),
                        output_field=FloatField(),
                    )
                )

            queryset = queryset.annotate(
                review_count=Count(
                    'reviews',
                    filter=Q(
                        reviews__review_type='restaurant',
                        reviews__is_visible=True,
                        reviews__is_verified_purchase=True,
                        reviews__moderation_status='APPROVED',
                    ),
                    distinct=True,
                ),
                delivered_order_count=Count(
                    'orders',
                    filter=Q(orders__status='DELIVERED'),
                    distinct=True,
                ),
                avg_prep_duration=Avg(
                    ExpressionWrapper(
                        F('orders__ready_at') - F('orders__preparing_at'),
                        output_field=DurationField(),
                    ),
                    filter=Q(
                        orders__ready_at__isnull=False,
                        orders__preparing_at__isnull=False,
                    ),
                ),
            )
            queryset = queryset.annotate(
                actual_prep_minutes=Case(
                    When(
                        avg_prep_duration__isnull=False,
                        then=Extract('avg_prep_duration', 'epoch') / Value(60.0),
                    ),
                    default=Cast(F('average_prep_time'), FloatField()),
                    output_field=FloatField(),
                )
            )

            if sort_by == 'distance' and lat and lng:
                queryset = queryset.order_by('distance')
            elif sort_by == 'rating':
                queryset = queryset.order_by('-rating', '-review_count')
            elif sort_by == 'prep_time':
                queryset = queryset.order_by('actual_prep_minutes')
            elif sort_by == 'recommended':
                global_avg = Review.objects.filter(
                    review_type='restaurant',
                    is_visible=True,
                    is_verified_purchase=True,
                    moderation_status='APPROVED',
                ).aggregate(avg=Avg('rating'))['avg'] or 4.0
                min_confidence_reviews = 10.0
                rating_float = Coalesce(
                    Cast(F('rating'), FloatField()),
                    Value(float(global_avg), output_field=FloatField()),
                    output_field=FloatField(),
                )
                review_count_float = Cast(F('review_count'), FloatField())
                weighted_rating = ExpressionWrapper(
                    (
                        (rating_float * review_count_float) +
                        (
                            Value(float(global_avg), output_field=FloatField()) *
                            Value(min_confidence_reviews, output_field=FloatField())
                        )
                    ) /
                    (
                        review_count_float +
                        Value(min_confidence_reviews, output_field=FloatField())
                    ),
                    output_field=FloatField(),
                )
                review_confidence = Case(
                    When(review_count__gte=50, then=Value(1.0)),
                    default=Cast(F('review_count'), FloatField()) / Value(50.0),
                    output_field=FloatField(),
                )
                prep_minutes = Case(
                    When(actual_prep_minutes__lt=1, then=Value(1.0)),
                    When(actual_prep_minutes__gt=60, then=Value(60.0)),
                    default=F('actual_prep_minutes'),
                    output_field=FloatField(),
                )
                quality = weighted_rating / Value(5.0, output_field=FloatField())
                speed = Value(1.0, output_field=FloatField()) - prep_minutes / Value(60.0, output_field=FloatField())
                trust = review_confidence
                if lat and lng:
                    MAX_RADIUS_M = 5000.0
                    proximity = Case(
                        When(geo_location__isnull=False, distance__lte=MAX_RADIUS_M,
                             then=Value(1.0) - F('distance') / Value(MAX_RADIUS_M, output_field=FloatField())),
                        When(geo_location__isnull=False, then=Value(0.0)),
                        default=Value(0.0),
                        output_field=FloatField(),
                    )
                    score_expr = ExpressionWrapper(
                        quality * Value(0.40, output_field=FloatField()) +
                        proximity * Value(0.30, output_field=FloatField()) +
                        speed * Value(0.20, output_field=FloatField()) +
                        trust * Value(0.10, output_field=FloatField()),
                        output_field=FloatField(),
                    )
                    queryset = queryset.annotate(score=score_expr).order_by('-score')
                else:
                    score_expr = ExpressionWrapper(
                        quality * Value(0.55, output_field=FloatField()) +
                        speed * Value(0.30, output_field=FloatField()) +
                        trust * Value(0.15, output_field=FloatField()),
                        output_field=FloatField(),
                    )
                    queryset = queryset.annotate(score=score_expr).order_by('-score')
        return queryset

    def list(self, request, *args, **kwargs):
        category = request.query_params.get('category', 'all').lower()
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        limit = request.query_params.get('limit')
        offset = request.query_params.get('offset')
        sort_by = request.query_params.get('sort_by', 'recommended')

        version = cache.get('restaurant_list_cache_version', 0)
        cache_key = stable_cache_key(
            "restaurant_list",
            {
                "version": version,
                "category": category,
                "lat": lat or "none",
                "lng": lng or "none",
                "limit": limit or "default",
                "offset": offset or "0",
                "sort_by": sort_by,
            },
        )
        cached_data = cache.get(cache_key)
        if cached_data:
            data = cached_data
        else:
            # An empty busy set makes get_is_busy return False without any
            # queries (its un-annotated fallback is 2 queries/row); the page
            # is re-stamped with fresh values below from one bounded call.
            self._list_busy_ids = set()
            response = super().list(request, *args, **kwargs)
            data = response.data
            cache.set(cache_key, data, 900)

        # is_busy is a live signal, so it is recomputed every request while
        # the row payload comes from cache — but scoped to the page rows,
        # not the whole restaurant table (DDIA Ch.1: no full-table
        # materialisation on the cache-hit path).
        results = data.get('results', data) if isinstance(data, dict) else data
        if results:
            from apps.restaurants.utils import compute_busy_restaurant_ids
            busy_ids = compute_busy_restaurant_ids([r['id'] for r in results])
            busy_lookup = {rid: True for rid in busy_ids}
            for r in results:
                r['is_busy'] = busy_lookup.get(r['id'], False)

        return Response(data)

    @action(detail=False, methods=['GET'])
    def mine(self, request):
        """Get the restaurant owned by the current user (or staff's restaurant).

        Pass ?restaurant=<id> to target one specific owned restaurant — the
        admin console uses this to open the vendor-style console for a
        restaurant an admin owns.
        """
        restaurant_id = request.query_params.get('restaurant')
        if restaurant_id:
            cache_key = f"restaurant_mine_{request.user.id}_{restaurant_id}"
            cached_data = cache.get(cache_key)
            if cached_data:
                return Response(cached_data)

            data = build_restaurant_mine_data(request.user, restaurant_id=restaurant_id)
            if not data:
                return Response(
                    {'error': 'Restaurant not found or not owned by you'},
                    status=status.HTTP_404_NOT_FOUND,
                )
            cache.set(cache_key, data, 300)
            return Response(data)

        cache_key = f"restaurant_mine_{request.user.id}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        data = build_restaurant_mine_data(request.user)
        if not data:
            return Response({}, status=200)

        cache.set(cache_key, data, 3600)
        return Response(data)

    @action(detail=True, methods=['PATCH'], url_path='profile')
    def update_profile(self, request, pk=None):
        """Allow restaurant owners to update public restaurant details and branding."""
        restaurant = self.get_object()
        if restaurant.owner_id != request.user.id:
            return Response(
                {'error': 'Only the restaurant owner can edit restaurant branding.'},
                status=status.HTTP_403_FORBIDDEN,
            )

        update_fields = {}
        for field in ('name', 'location', 'description', 'category'):
            if field in request.data:
                update_fields[field] = request.data.get(field)

        for field in ('image', 'logo'):
            uploaded = request.FILES.get(field)
            if uploaded is not None:
                if uploaded.size > 5 * 1024 * 1024:
                    return Response(
                        {field: 'Image must be 5MB or smaller.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if uploaded.content_type not in {'image/jpeg', 'image/png', 'image/webp'}:
                    return Response(
                        {field: 'Only JPEG, PNG, and WebP images are allowed.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                update_fields[field] = uploaded

        latitude = request.data.get('latitude')
        longitude = request.data.get('longitude')
        if latitude is not None or longitude is not None:
            update_fields['latitude'] = latitude
            update_fields['longitude'] = longitude

        from domain.services.admin_services import ManageRestaurantService
        restaurant = ManageRestaurantService.update_restaurant(str(restaurant.id), **update_fields)
        invalidate_restaurant_cache(restaurant)
        return Response(RestaurantSerializer(restaurant, context={'request': request}).data)

    @action(detail=True, methods=['GET'], url_path='drivers')
    def list_drivers(self, request, pk=None):
        """List all drivers visible to this restaurant (platform + assigned).
        Each driver gets a `recommended` boolean — the best available platform driver
        (highest priority, has capacity) is pre-selected in the UI.
        """
        from domain.services.permissions import (
            check_restaurant_permission,
            restaurant_visible_drivers,
        )
        restaurant = self.get_object()
        if not check_restaurant_permission(request.user, restaurant):
            return Response({'error': 'Not authorized to view drivers'}, status=403)
        drivers = restaurant_visible_drivers(restaurant)

        recommended_id = None
        for d in drivers:
            if d.driver_type == 'platform' and d.current_active_orders < d.max_active_orders:
                recommended_id = str(d.id)
                break

        return Response(DriverSerializer(drivers, many=True, context={'recommended_id': recommended_id}).data)

    @action(detail=True, methods=['POST'], url_path='register-temp-driver')
    def register_temp_driver(self, request, pk=None):
        """
        Register a user as a temporary driver for this restaurant.
        Body: { user_id }
        """
        from domain.services.permissions import check_restaurant_permission
        user_id = request.data.get('user_id')
        if not user_id:
            return Response({'error': 'user_id is required'}, status=400)

        restaurant = self.get_object()
        if not check_restaurant_permission(request.user, restaurant):
            return Response({'error': 'Only the restaurant owner or staff can register temp drivers'}, status=403)

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
            fee, service_fee, fee_log = PricingService.calculate_delivery_fee(
                restaurant,
                delivery_location,
                skip_log=True,
            )
            
            # Extract breakdown from fee_log
            breakdown = {
                "base_fee": float(fee_log.base_fee_used) if fee_log else 1000.0,
                "adjustments": fee_log.adjustments if fee_log else {}
            }
            
            return Response({
                "delivery_fee": float(fee),
                "service_fee": float(service_fee),
                "currency": "TSh",
                "breakdown": breakdown,
                "restaurant_latitude": restaurant.geo_location.y if restaurant.geo_location else None,
                "restaurant_longitude": restaurant.geo_location.x if restaurant.geo_location else None,
            })
        except (ValueError, TypeError):
            return Response({'error': 'Invalid lat or lng'}, status=400)

    @action(detail=False, methods=['GET'], url_path='pending-batches')
    def pending_batches(self, request):
        """
        Get batches waiting for temp driver that contain orders from this restaurant.
        Used by restaurant owner to see batches needing a driver.
        """
        from datetime import timedelta

        from django.conf import settings
        from django.utils import timezone

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

    @action(detail=True, methods=['PATCH'], url_path='prep-time')
    def update_prep_time(self, request, pk=None):
        """Update the restaurant's average preparation time (vendor-facing)."""
        restaurant = self.get_object()
        owner = restaurant.owner
        if request.user != owner and not request.user.is_staff:
            from apps.restaurants.models import SectionStaff
            if not SectionStaff.objects.filter(user=request.user, section__restaurant=restaurant).exists():
                return Response({'error': 'Not authorized'}, status=403)
        try:
            value = int(request.data.get('average_prep_time', 0))
            if value < 1 or value > 180:
                return Response({'error': 'Prep time must be between 1 and 180 minutes'}, status=400)
            restaurant.average_prep_time = value
            restaurant.save(update_fields=['average_prep_time'])
        except (TypeError, ValueError):
            return Response({'error': 'Invalid prep time value'}, status=400)
        invalidate_restaurant_cache(restaurant)
        return Response({'average_prep_time': restaurant.average_prep_time})

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
            'payment',
        ).prefetch_related(
            'items', 'items__food', 'items__section',
            'batch__orders',
            'batch__section_statuses',
            'pool__members',
            'pool__members__user',
            # OrderSerializer.get_is_reviewed reads obj.reviews; prefetch it so
            # the list endpoint doesn't fire one EXISTS query per order (N+1).
            'reviews',
        ).order_by('-created_at')
        
        if user.serialized_role == 'customer':
            queryset = queryset.filter(customer=user)
        elif user.serialized_role in ('vendor', 'vendor_staff'):
            from apps.restaurants.models import SectionStaff
            staff_sections = SectionStaff.objects.filter(user=user).values_list('section_id', flat=True)
            if staff_sections.exists():
                queryset = queryset.filter(
                    items__section_id__in=list(staff_sections),
                    is_paid=True
                ).distinct()
            else:
                queryset = queryset.filter(restaurant__owner=user, is_paid=True)
        elif user.role == 'driver':
            queryset = queryset.filter(driver__user=user, is_paid=True)
        else:
            queryset = queryset.filter(is_paid=True)

        # Admin console: scope the global admin order feed to one restaurant
        # when the reused vendor widgets pass ?restaurant=<id>.
        restaurant_filter = self.request.query_params.get('restaurant')
        if restaurant_filter and user.role == 'admin':
            queryset = queryset.filter(restaurant_id=restaurant_filter)

        if self.request.query_params.get('active_only') in ('1', 'true', 'True'):
            queryset = queryset.exclude(
                status__in=['DELIVERED', 'CANCELLED', 'EXPIRED', 'REFUNDED', 'RESOLVED']
            )
        return queryset

    @action(detail=False, methods=['GET'])
    def stats(self, request):
        # An admin running a restaurant console requests stats for a specific
        # restaurant; key the cache on it so admins don't collide/poison vendor
        # stats and vice-versa.
        _stats_restaurant = (
            request.query_params.get('restaurant')
            if request.user.role == 'admin' else None
        )
        cache_key = (
            f"user_stats_{request.user.id}_{request.user.role}"
            f"_{_stats_restaurant or 'none'}"
        )
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        from django.db.models import Avg, Count, DurationField, ExpressionWrapper, Sum
        from django.utils import timezone
        now = timezone.now()
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timezone.timedelta(days=1)
        
        user = request.user
        
        data = {}
        try:
            if user.role == 'admin' and _stats_restaurant:
                restaurant = Restaurant.objects.filter(id=_stats_restaurant).first()
                if restaurant is None:
                    return Response({"error": "Restaurant not found"}, status=404)
                orders = Order.objects.filter(
                    restaurant=restaurant,
                    created_at__gte=start_of_day,
                    created_at__lt=end_of_day,
                    is_paid=True,
                )
                revenue = orders.filter(status='DELIVERED').aggregate(Sum('total_amount'))['total_amount__sum'] or 0
                count = orders.count()
                cancelled_count = orders.filter(status='CANCELLED').count()
                vendor_fault_cancellations = orders.filter(cancellation_fault='VENDOR').count()
                public_reputation_cancellations = orders.filter(affects_restaurant_reputation=True).count()
                cancellation_rate = round((cancelled_count / count) * 100, 2) if count else 0
                configured_prep_minutes = restaurant.average_prep_time if restaurant else 20

                avg_prep_duration = orders.filter(
                    confirmed_at__isnull=False,
                    ready_at__isnull=False,
                ).exclude(
                    models.Q(ready_at__lte=models.F('confirmed_at'))
                ).annotate(
                    prep_duration=ExpressionWrapper(
                        models.F('ready_at') - models.F('confirmed_at'),
                        output_field=DurationField()
                    )
                ).aggregate(avg=Avg('prep_duration'))['avg']
                avg_seconds = avg_prep_duration.total_seconds() if avg_prep_duration else 0
                avg_minutes = int(avg_seconds // 60)
                avg_secs = int(avg_seconds % 60)
                avg_prep = f"{avg_minutes:02d}:{avg_secs:02d}" if avg_seconds > 0 else "—"

                # A restaurant's money lives in its section wallets.
                wallet_balance = float(
                    Wallet.objects.filter(section__restaurant=restaurant)
                    .aggregate(Sum('balance'))['balance__sum'] or 0
                )
                pending_payout = float(
                    Wallet.objects.filter(section__restaurant=restaurant)
                    .aggregate(Sum('pending_balance'))['pending_balance__sum'] or 0
                )

                data = {
                    "revenue_today": float(revenue),
                    "orders_today": count,
                    "avg_prep_time": f"{configured_prep_minutes} min",
                    "configured_prep_time": configured_prep_minutes,
                    "actual_avg_prep_time": avg_prep,
                    "wallet_balance": wallet_balance,
                    "pending_payout": pending_payout,
                    "cancelled_orders_today": cancelled_count,
                    "vendor_fault_cancellations_today": vendor_fault_cancellations,
                    "public_reputation_cancellations_today": public_reputation_cancellations,
                    "cancellation_rate_today": cancellation_rate,
                }
            elif user.serialized_role in ('vendor', 'vendor_staff'):
                from apps.restaurants.models import SectionStaff
                staff_section_ids = list(SectionStaff.objects.filter(user=user).values_list('section_id', flat=True))
                if staff_section_ids:
                    wallet, _ = Wallet.objects.get_or_create(section_id=staff_section_ids[0])
                    restaurant = Restaurant.objects.filter(
                        sections__id__in=staff_section_ids
                    ).distinct().first()
                    orders = Order.objects.filter(
                        items__section_id__in=staff_section_ids,
                        created_at__gte=start_of_day,
                        created_at__lt=end_of_day,
                        is_paid=True,
                    ).distinct()
                else:
                    wallet, _ = Wallet.objects.get_or_create(user=user)
                    restaurant = Restaurant.objects.filter(owner=user).first()
                    orders = Order.objects.filter(
                        restaurant__owner=user,
                        created_at__gte=start_of_day,
                        created_at__lt=end_of_day,
                        is_paid=True,
                    )
                revenue = orders.filter(status='DELIVERED').aggregate(Sum('total_amount'))['total_amount__sum'] or 0
                count = orders.count()
                cancelled_count = orders.filter(status='CANCELLED').count()
                vendor_fault_cancellations = orders.filter(cancellation_fault='VENDOR').count()
                public_reputation_cancellations = orders.filter(affects_restaurant_reputation=True).count()
                cancellation_rate = round((cancelled_count / count) * 100, 2) if count else 0
                configured_prep_minutes = restaurant.average_prep_time if restaurant else 20

                avg_prep_duration = orders.filter(
                    confirmed_at__isnull=False,
                    ready_at__isnull=False,
                ).exclude(
                    models.Q(ready_at__lte=models.F('confirmed_at'))
                ).annotate(
                    prep_duration=ExpressionWrapper(
                        models.F('ready_at') - models.F('confirmed_at'),
                        output_field=DurationField()
                    )
                ).aggregate(avg=Avg('prep_duration'))['avg']

                avg_seconds = avg_prep_duration.total_seconds() if avg_prep_duration else 0

                avg_minutes = int(avg_seconds // 60)
                avg_secs = int(avg_seconds % 60)
                avg_prep = f"{avg_minutes:02d}:{avg_secs:02d}" if avg_seconds > 0 else "—"

                # Aggregate wallet balance if owner
                wallet_balance = float(wallet.balance)
                pending_payout = float(wallet.pending_balance)
                if not staff_section_ids and restaurant:
                    from apps.wallets.models import Wallet as WalletModel
                    section_balances = WalletModel.objects.filter(
                        section__restaurant=restaurant
                    ).aggregate(Sum('balance'))['balance__sum'] or 0
                    wallet_balance += float(section_balances)
                    
                    section_pending = WalletModel.objects.filter(
                        section__restaurant=restaurant
                    ).aggregate(Sum('pending_balance'))['pending_balance__sum'] or 0
                    pending_payout += float(section_pending)

                data = {
                    "revenue_today": float(revenue),
                    "orders_today": count,
                    "avg_prep_time": f"{configured_prep_minutes} min",
                    "configured_prep_time": configured_prep_minutes,
                    "actual_avg_prep_time": avg_prep,
                    "wallet_balance": wallet_balance,
                    "pending_payout": pending_payout,
                    "cancelled_orders_today": cancelled_count,
                    "vendor_fault_cancellations_today": vendor_fault_cancellations,
                    "public_reputation_cancellations_today": public_reputation_cancellations,
                    "cancellation_rate_today": cancellation_rate,
                }
            elif user.role == 'driver':
                wallet, _ = Wallet.objects.get_or_create(user=user)
                driver, _ = Driver.objects.get_or_create(
                    user=user,
                    defaults={'status': 'OFFLINE'}
                )
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
                    "can_assign": driver.can_assign,
                    "driver_latitude": driver.location.y if driver.location else None,
                    "driver_longitude": driver.location.x if driver.location else None,
                }
            else:
                return Response({"error": "Stats not available for this role"}, status=400)
            
            cache.set(cache_key, data, 300)
            return Response(data)
        except Exception as e:
            logger.exception("Error in stats action: %s", str(e))
            return Response({"error": str(e)}, status=500)

    @action(detail=False, methods=['GET'], url_path='available-jobs')
    def available_jobs(self, request):
        # Cap limit outside DRF pagination — this endpoint slices manually.
        # A bogus value (limit=abc) must 400, not raise an uncaught 500, and
        # limit=100000 must not serialize the whole READY table (DDIA Ch.1).
        try:
            limit = min(int(request.query_params.get('limit', 20)), 100)
            offset = max(int(request.query_params.get('offset', 0)), 0)
        except (TypeError, ValueError):
            return Response({"error": "limit and offset must be integers"}, status=400)

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
        ).prefetch_related('items', 'items__food', 'items__section').order_by('-created_at')[offset:offset + limit]

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

                from domain.services.permissions import can_serve_restaurant
                if not can_serve_restaurant(driver, order.restaurant_id):
                    return Response({"error": "Driver cannot serve this restaurant"}, status=403)

                if order.batch_id:
                    batch = AssignBatchService.execute(order.batch_id, driver.id, request.user)
                    if batch.status == 'DISPATCHED':
                        order.refresh_from_db()
                    else:
                        # Fallback to single-order assignment
                        order = AssignDriverService.execute(pk, driver.id, request.user)
                else:
                    order = AssignDriverService.execute(pk, driver.id, request.user)

                # The claimed job must disappear from EVERY driver's cached
                # available-jobs list, not just this driver's — otherwise a
                # rival sees the job for up to 15s and gets a hard 400 on tap
                # (DDIA Ch.9 monotonic reads). Runs after commit.
                transaction.on_commit(_invalidate_available_jobs_cache)

                return Response(OrderSerializer(order).data)
        except Driver.DoesNotExist:
            return Response({"error": "You are not registered as a driver"}, status=403)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)
        except Exception as e:
            return Response({"error": f"Claim failed: {str(e)}"}, status=409)

    @action(detail=True, methods=['POST'])
    def confirm(self, request, pk=None):
        _log = logging.getLogger(__name__)
        try:
            from domain.services.permissions import check_restaurant_permission
            order = Order.objects.get(id=pk)
            # check_restaurant_permission grants admins too, so an admin who
            # owns (or escalates on) a restaurant can run the kitchen.
            if not check_restaurant_permission(request.user, order.restaurant):
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
            from domain.services.permissions import check_restaurant_permission
            order = Order.objects.get(id=pk)
            if not check_restaurant_permission(request.user, order.restaurant):
                return Response({"error": "Not authorized to prepare this order"}, status=403)

            order = StartPreparingService.execute(pk, request.user)
            return Response(OrderSerializer(order).data)
        except InvalidStateTransitionError as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'])
    def ready(self, request, pk=None):
        try:
            from domain.services.permissions import check_restaurant_permission
            order = Order.objects.get(id=pk)
            if not check_restaurant_permission(request.user, order.restaurant):
                return Response({"error": "Not authorized to mark this order as ready"}, status=403)
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
            _release_payment_sync(order)
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (ValueError, InvalidStateTransitionError) as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='complete-pickup')
    def complete_pickup(self, request, pk=None):
        try:
            order = CompletePickupService.execute(pk, request.user)
            _release_payment_sync(order)
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (ValueError, InvalidStateTransitionError) as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='rider-cancel')
    def rider_cancel(self, request, pk=None):
        reason = request.data.get('reason', 'Cancelled by driver')
        category = request.data.get('category', 'driver_cancelled')
        from domain.services.order_services import CancelOrderService
        try:
            order = CancelOrderService.execute(pk, request.user, reason=reason, category=category)
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
            reason = request.data.get('reason', 'Rejected by restaurant')
            category = request.data.get('category', 'vendor_rejected')
            order = CancelOrderService.execute(pk, request.user, reason=reason, category=category)
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (InvalidStateTransitionError, ValueError) as e:
            return Response({"error": str(e)}, status=400)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

    @action(detail=True, methods=['POST'])
    def cancel(self, request, pk=None):
        try:
            from domain.services.order_services import CancelOrderService
            order = CancelOrderService.execute(
                pk,
                request.user,
                reason=request.data.get('reason', 'Cancelled by customer'),
                category=request.data.get('category', 'customer_changed_mind'),
            )
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (InvalidStateTransitionError, ValueError) as e:
            return Response({"error": str(e)}, status=400)
        except Exception as e:
            return Response({"error": str(e)}, status=500)

    @action(detail=True, methods=['POST'], url_path='cancel-item')
    def cancel_item(self, request, pk=None):
        try:
            from domain.services.orders.order_cancellation import CancelOrderItemService
            item_id = request.data.get('item_id')
            if not item_id:
                return Response({"error": "item_id is required"}, status=400)
            reason = request.data.get('reason', 'Item cancelled by restaurant')
            item = CancelOrderItemService.execute(
                pk,
                item_id,
                request.user,
                reason=reason,
                category=request.data.get('category', 'item_unavailable'),
            )
            order = Order.objects.get(id=pk)
            return Response(OrderSerializer(order).data)
        except PermissionError as e:
            return Response({"error": str(e)}, status=403)
        except (ValueError, InvalidStateTransitionError) as e:
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

        from domain.services.permissions import check_restaurant_permission
        if not check_restaurant_permission(request.user, order.restaurant):
            return Response({'error': 'Only the restaurant owner or staff can assign temp drivers'}, status=403)

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

    @action(detail=True, methods=['GET'], url_path='pickup-payment')
    def pickup_payment(self, request, pk=None):
        """
        What the driver owes this restaurant, and whether it is already paid.

        Powers the pickup payment sheet: the driver sees the amount and enters the
        restaurant's Lipa Namba / phone number.
        """
        from domain.services.orders.pickup_payment import restaurant_net_share

        try:
            order = Order.objects.select_related('restaurant').get(id=pk)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=404)

        from apps.payments.models import PickupDisbursement

        disbursement = PickupDisbursement.objects.filter(order=order).first()

        return Response({
            'order_id': str(order.id),
            'short_id': str(order.id)[:8].upper(),
            'status': order.status,
            'restaurant_id': str(order.restaurant_id),
            'restaurant_name': order.restaurant.name,
            'restaurant_phone': (
                order.restaurant.owner.phone if order.restaurant.owner else None
            ),
            'amount': str(restaurant_net_share(order)),
            'is_paid': bool(disbursement and disbursement.status == PickupDisbursement.STATUS_SUCCESS),
            'payment': (
                {
                    'status': disbursement.status,
                    'amount': str(disbursement.amount),
                    'account_number': disbursement.account_number,
                    'network': disbursement.network,
                    'gateway_reference': disbursement.gateway_reference,
                    'failure_reason': disbursement.failure_reason,
                }
                if disbursement
                else None
            ),
        })

    @action(detail=True, methods=['POST'], url_path='pay-restaurant')
    def pay_restaurant(self, request, pk=None):
        """
        Pay the restaurant at the counter, before collecting the food.

        Body: { account_number (Lipa Namba / phone), network? }

        Sends the restaurant's net share to their mobile money from escrow the
        customer already paid. Idempotent — the unique disbursement row is claimed
        before the gateway is called, so repeated taps cannot double-pay.
        """
        from domain.services.orders.pickup_payment import PayRestaurantAtPickupService
        from domain.utils import is_valid_tz_phone

        account_number = (request.data.get('account_number') or '').strip()
        network = (request.data.get('network') or '').strip()

        if not account_number:
            return Response(
                {'error': 'account_number is required', 'code': 'MISSING_ACCOUNT'},
                status=400,
            )

        if not is_valid_tz_phone(account_number):
            return Response({
                'error': 'Enter a valid Lipa Namba or phone number (e.g. 0712 345 678).',
                'code': 'INVALID_ACCOUNT',
            }, status=400)

        try:
            disbursement = PayRestaurantAtPickupService.execute(
                order_id=str(pk),
                actor=request.user,
                account_number=account_number,
                network=network,
            )
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=404)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except Exception as e:
            logger.error(f"Pickup payment failed for order {pk}: {e}", exc_info=True)
            return Response({'error': f'Payment failed: {e}'}, status=502)

        return Response({
            'success': disbursement.status == 'SUCCESS',
            'status': disbursement.status,
            'amount': str(disbursement.amount),
            'account_number': disbursement.account_number,
            'network': disbursement.network,
            'gateway_reference': disbursement.gateway_reference,
            'error': disbursement.failure_reason or None,
        }, status=200 if disbursement.status == 'SUCCESS' else 502)

    @action(detail=True, methods=['POST'])
    def pay(self, request, pk=None):
        from domain.services.payment_services import InitiatePaymentService
        phone = request.data.get('phone')
        bank_code = request.data.get('bank_code')
        try:
            result = InitiatePaymentService.execute(
                payable_type='order',
                payable_id=pk,
                phone=phone,
                bank_code=bank_code,
                actor=request.user,
            )
            payment = result['payment']
            gateway_data = result.get('gateway_response', {})

            push_sent = gateway_data.get('success', False)
            base_response = {
                "success": push_sent,
                "payment_id": str(payment.id), 
                "status": payment.status,
                "is_push": result.get('is_push', False),
            }

            if push_sent:
                base_response["provider_reference"] = gateway_data.get('provider_reference')
                base_response["message"] = gateway_data.get('message')
                base_response["instant_success"] = gateway_data.get('instant_success')
                if gateway_data.get('params'):
                    base_response["params"] = gateway_data['params']
                if gateway_data.get('endpoint'):
                    base_response["endpoint"] = gateway_data['endpoint']
            else:
                base_response["error"] = 'Payment initiation failed'
                base_response["code"] = gateway_data.get('code')

            return Response(base_response)
        except Exception:
            logger.exception("Order payment initiation failed")
            return Response({"error": "Payment initiation failed."}, status=400)

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
        ).prefetch_related('items', 'items__food', 'items__section').all()
        
        return Response(OrderSerializer(batch_orders, many=True).data)

class DisputeViewSet(viewsets.ModelViewSet):
    queryset = Dispute.objects.all()
    serializer_class = DisputeSerializer
    permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
        return Response(
            {"error": "Create disputes from the order dispute action."},
            status=405,
        )

    def update(self, request, *args, **kwargs):
        return Response({"error": "Use the resolve action to update disputes."}, status=405)

    def partial_update(self, request, *args, **kwargs):
        return Response({"error": "Use the resolve action to update disputes."}, status=405)

    def destroy(self, request, *args, **kwargs):
        return Response({"error": "Disputes cannot be deleted."}, status=405)

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
        all_vendors = list(
            Restaurant.objects.filter(id__in=vendor_ids).prefetch_related('sections__foods')
        )

        # One bounded busy computation for every restaurant row serialized
        # below — without it RestaurantListSerializer.get_is_busy falls back
        # to 2 existence queries per row (DDIA Ch.1 N+1).
        from apps.restaurants.utils import compute_busy_restaurant_ids
        busy_ids = compute_busy_restaurant_ids(
            list(vendor_ids | {v.id for v in all_vendors})
        )
        restaurant_context = {'request': request, 'busy_ids': busy_ids}

        return Response({
            "restaurants": RestaurantListSerializer(restaurants_qs, many=True, context=restaurant_context).data,
            "products": FoodSerializer(products_qs, many=True, context={'request': request}).data,
            "marketplace": MarketplaceProductSerializer(marketplace_qs, many=True, context={'request': request}).data,
            "vendors": RestaurantListSerializer(all_vendors, many=True, context=restaurant_context).data
        })


class AppConfigView(APIView):
    """Public endpoint for app version config and update URLs."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({
            "minimum_version": os.getenv("MIN_APP_VERSION", "1.0.1+7"),
            "latest_version": os.getenv("LATEST_APP_VERSION", "1.0.1+7"),
            "update_url": os.getenv(
                "APP_UPDATE_URL",
                "https://play.google.com/store/apps/details?id=com.mwanachuoshop.app",
            ),
        })
