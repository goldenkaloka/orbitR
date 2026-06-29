from decimal import Decimal

from api.serializers.utils import parse_coordinate_dict
from apps.core.models import Notification
from apps.delivery.models import DeliveryProof, Driver
from apps.orders.models import BatchSectionStatus, Dispute, Order, OrderItem
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from domain.events.bus import EventBus
from domain.events.types import OrderCreated
from rest_framework import serializers


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'title', 'body', 'data', 'is_read', 'created_at']


class UserSerializer(serializers.ModelSerializer):
    is_driver = serializers.SerializerMethodField()
    driver_id = serializers.SerializerMethodField()
    serialized_role = serializers.SerializerMethodField()
    is_restaurant_owner = serializers.SerializerMethodField()
    is_stall_staff = serializers.SerializerMethodField()
    can_be_driver = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'phone', 'role', 'serialized_role',
            'is_driver', 'driver_id', 'is_restaurant_owner', 'is_stall_staff',
            'can_be_driver'
        ]

    def get_is_driver(self, obj):
        annotated = getattr(obj, 'has_driver_profile', None)
        if annotated is not None:
            return annotated
        from apps.delivery.models import Driver
        return Driver.objects.filter(user=obj).exists()

    def get_driver_id(self, obj):
        if hasattr(obj, 'driver_profile_id'):
            annotated = getattr(obj, 'driver_profile_id', None)
            return str(annotated) if annotated else None
        from apps.delivery.models import Driver
        try:
            driver = Driver.objects.only('id').get(user=obj)
            return str(driver.id)
        except Driver.DoesNotExist:
            return None

    def get_serialized_role(self, obj):
        has_section_staff = getattr(obj, 'has_section_staff', None)
        if obj.role == 'customer' and has_section_staff is not None:
            return 'vendor_staff' if has_section_staff else obj.role
        return obj.serialized_role

    def get_is_restaurant_owner(self, obj):
        annotated = getattr(obj, 'owns_restaurant', None)
        if annotated is not None:
            return annotated
        from apps.restaurants.models import Restaurant
        return Restaurant.objects.filter(owner=obj).exists()

    def get_is_stall_staff(self, obj):
        annotated = getattr(obj, 'has_section_staff', None)
        if annotated is not None:
            return annotated
        from apps.restaurants.models import SectionStaff
        return SectionStaff.objects.filter(user=obj).exists()

    def get_can_be_driver(self, obj):
        return (
            obj.role == 'customer'
            and not self.get_is_driver(obj)
            and not self.get_is_restaurant_owner(obj)
            and not self.get_is_stall_staff(obj)
        )

class FoodSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    vendor_id = serializers.CharField(source='section.restaurant.id', read_only=True)
    vendor_name = serializers.CharField(source='section.restaurant.name', read_only=True)

    class Meta:
        model = Food
        fields = ['id', 'name', 'description', 'price', 'category', 'is_available', 'image', 'image_url', 'vendor_id', 'vendor_name', 'section']

    def validate(self, attrs):
        section = attrs.get('section')
        if section:
            request = self.context.get('request')
            if request and request.user:
                from apps.restaurants.models import Restaurant, SectionStaff
                try:
                    restaurant = Restaurant.objects.get(owner=request.user)
                except Restaurant.DoesNotExist:
                    staff_sec = SectionStaff.objects.filter(user=request.user).select_related('section__restaurant').first()
                    restaurant = staff_sec.section.restaurant if staff_sec else None

                if restaurant and section.restaurant != restaurant:
                    raise serializers.ValidationError({"section": "The selected section does not belong to your restaurant."})
        elif self.instance is None:
            raise serializers.ValidationError({"section": "Section (Stall) is required."})
        return attrs

    def get_image_url(self, obj):
        image = getattr(obj, 'image', None) if not isinstance(obj, dict) else obj.get('image')
        if image:
            try:
                return image.url
            except (AttributeError, ValueError):
                return None
        return None


class RestaurantSectionSerializer(serializers.ModelSerializer):
    food_count = serializers.SerializerMethodField()

    class Meta:
        model = RestaurantSection
        fields = ['id', 'name', 'specialization', 'description', 'image',
                  'is_active', 'sort_order', 'prep_time', 'food_count']

    def get_food_count(self, obj):
        annotated = getattr(obj, 'available_food_count', None)
        if annotated is not None:
            return annotated
        if hasattr(obj, '_prefetched_objects_cache') and 'foods' in obj._prefetched_objects_cache:
            return sum(1 for food in obj.foods.all() if food.is_available)
        return obj.foods.filter(is_available=True).count()


class BatchSectionStatusSerializer(serializers.ModelSerializer):
    section_name = serializers.CharField(source='section.name', read_only=True)
    section_specialization = serializers.CharField(source='section.specialization', read_only=True)

    class Meta:
        model = BatchSectionStatus
        fields = ['id', 'batch', 'section', 'section_name', 'section_specialization', 'status', 'estimated_ready_at', 'started_prepping_at', 'updated_at']
        read_only_fields = ['id', 'section', 'section_name', 'section_specialization', 'updated_at']


class RestaurantListSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    foods = serializers.SerializerMethodField()
    sections = RestaurantSectionSerializer(many=True, read_only=True)
    is_busy = serializers.SerializerMethodField()

    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()

    class Meta:
        model = Restaurant
        fields = [
            'id', 'name', 'description', 'image', 'image_url', 
            'location', 'geo_location', 'latitude', 'longitude', 'category', 'price_range', 'rating', 
            'is_active', 'is_busy', 'foods', 'sections', 'average_prep_time'
        ]

    def get_latitude(self, obj):
        return obj.geo_location.y if obj.geo_location else None

    def get_longitude(self, obj):
        return obj.geo_location.x if obj.geo_location else None


    def get_image_url(self, obj):
        image = getattr(obj, 'image', None) if not isinstance(obj, dict) else obj.get('image')
        if image:
            try:
                return image.url
            except (AttributeError, ValueError):
                return None
        return None

    def get_foods(self, obj):
        user = self.context.get('_user')
        if user is None:
            request = self.context.get('request')
            user = getattr(request, 'user', None) if request else None
        role = getattr(user, 'serialized_role', None) if user else None
        if user and role in ('vendor', 'vendor_staff', 'admin'):
            qs = obj.foods.all()
        else:
            qs = obj.foods.filter(is_available=True)
        return FoodSerializer(qs, many=True).data

    def get_is_busy(self, obj):
        busy_ids = self.context.get('busy_ids')
        if busy_ids is not None:
            return str(obj.id) in busy_ids

        from apps.restaurants.utils import compute_busy_restaurant_ids
        return str(obj.id) in compute_busy_restaurant_ids([obj.id])


class RestaurantSerializer(RestaurantListSerializer):
    sections = RestaurantSectionSerializer(many=True, read_only=True)

    class Meta(RestaurantListSerializer.Meta):
        fields = RestaurantListSerializer.Meta.fields + ['sections']
class OrderItemSerializer(serializers.ModelSerializer):
    food_name = serializers.SerializerMethodField()
    section_name = serializers.CharField(source='section.name', read_only=True)
    section_id = serializers.CharField(source='section.id', read_only=True)

    class Meta:
        model = OrderItem
        fields = ['food', 'food_name', 'quantity', 'price', 'section_id', 'section_name']
        extra_kwargs = {'price': {'read_only': True}}

    def get_food_name(self, obj):
        return obj.food.name if obj.food else "Marketplace Item"



class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True)
    restaurant_name = serializers.SerializerMethodField()
    restaurant_latitude = serializers.SerializerMethodField()
    restaurant_longitude = serializers.SerializerMethodField()
    delivery_latitude = serializers.SerializerMethodField()
    delivery_longitude = serializers.SerializerMethodField()
    # Pre-fetched route geometry embedded in the order response so the client
    # never needs a second HTTP round-trip to Mapbox Directions on page open.
    route_geometry = serializers.SerializerMethodField()
    payment_status = serializers.ReadOnlyField(source='payment.status')

    rider_name = serializers.ReadOnlyField(source='driver.user.username')
    rider_phone = serializers.ReadOnlyField(source='driver.user.phone')
    rider_id = serializers.ReadOnlyField(source='driver.user_id')
    customer_name = serializers.ReadOnlyField(source='customer.username')
    customer_phone = serializers.ReadOnlyField(source='customer.phone')
    restaurant_phone = serializers.ReadOnlyField(source='restaurant.owner.phone')
    
    batch = serializers.SerializerMethodField()
    original_delivery_fee = serializers.SerializerMethodField()
    discount_amount = serializers.SerializerMethodField()
    route_sequence = serializers.ReadOnlyField()
    pool_id = serializers.SerializerMethodField()
    group_breakdown = serializers.SerializerMethodField()
    
    class Meta:
        model = Order
        fields = [
            'id', 'status', 'is_paid', 'items_subtotal', 'delivery_fee', 'service_fee', 'total_amount',
            'original_delivery_fee', 'discount_applied', 'discount_amount',
            'restaurant', 'restaurant_name', 'restaurant_latitude', 'restaurant_longitude',
            'restaurant_phone', 'customer_name', 'customer_phone',
            'items', 'fulfillment_type', 'delivery_address', 'delivery_location', 'delivery_latitude', 'delivery_longitude',
            'notes', 'rider_name', 'rider_phone', 'rider_id', 'created_at', 'delivery_otp', 'group_breakdown',
            'batch', 'route_sequence', 'is_pool_order', 'pool_id', 'route_geometry', 'payment_status',
        ]

        read_only_fields = ['status', 'items_subtotal', 'delivery_fee', 'service_fee', 'total_amount', 'delivery_otp',
                           'original_delivery_fee', 'discount_applied', 'discount_amount']


    def get_restaurant_latitude(self, obj):
        return obj.restaurant.geo_location.y if obj.restaurant and obj.restaurant.geo_location else None

    def get_restaurant_longitude(self, obj):
        return obj.restaurant.geo_location.x if obj.restaurant and obj.restaurant.geo_location else None

    def get_delivery_latitude(self, obj):
        return obj.delivery_location.y if obj.delivery_location else None

    def get_delivery_longitude(self, obj):
        return obj.delivery_location.x if obj.delivery_location else None

    def get_route_geometry(self, obj):
        """
        Returns a pre-fetched [[lng, lat], ...] polyline for the static
        restaurant → delivery route, cached in Redis for 2 hours.

        The client uses this to draw the route instantly on map load with
        zero extra HTTP calls. Falls back to None gracefully — the client
        will fetch from Mapbox Directions API as a fallback.
        """
        from apps.core.cache_keys import stable_cache_key
        from django.core.cache import cache

        if getattr(obj, 'fulfillment_type', 'DELIVERY') == 'PICKUP':
            return None

        res = obj.restaurant
        if not res or not res.geo_location or not obj.delivery_location:
            return None

        res_lng = round(res.geo_location.x, 6)
        res_lat = round(res.geo_location.y, 6)
        dest_lng = round(obj.delivery_location.x, 6)
        dest_lat = round(obj.delivery_location.y, 6)

        cache_key = stable_cache_key('route_geom', {
            'from': [res_lng, res_lat],
            'to': [dest_lng, dest_lat],
        })

        return cache.get(cache_key)

            # Cache for 2 hours — restaurant/delivery coords don't change per order
    def get_restaurant_name(self, obj):
        return obj.restaurant.name if obj.restaurant else "Marketplace Order"

    def get_pool_id(self, obj):
        return str(obj.pool_id) if obj.pool_id else None

    def get_group_breakdown(self, obj):
        raw = getattr(obj, 'group_breakdown', None)
        pool = getattr(obj, 'pool', None)
        if not pool:
            # Single-order group_breakdown — wrap in list for frontend
            if raw:
                return [raw] if isinstance(raw, dict) else raw
            return None

        breakdown = []
        for member in pool.members.all():
            items_data = member.items or []
            breakdown.append({
                'member_id': str(member.id),
                'user_id': str(member.user_id) if member.user_id else None,
                'username': member.user.username if member.user_id else 'Member',
                'subtotal': float(member.items_subtotal) if member.items_subtotal else 0,
                'delivery_share': float(member.delivery_fee_share) if member.delivery_fee_share else 0,
                'items': [
                    {
                        'food_name': i.get('name', 'Item'),
                        'food_id': i.get('food_id', ''),
                        'quantity': i.get('quantity', 0),
                        'price': float(i.get('price', 0)),
                    }
                    for i in items_data
                ],
            })
        return breakdown if breakdown else None

    def get_batch(self, obj):
        if not obj.batch:
            return None
        
        request = self.context.get('request')
        role = getattr(request, 'user', None).role if request and hasattr(request, 'user') else None

        order_count = getattr(obj.batch, 'orders_count', None)
        if order_count is None:
            if hasattr(obj.batch, '_prefetched_objects_cache') and 'orders' in obj.batch._prefetched_objects_cache:
                order_count = len(obj.batch.orders.all())
            else:
                order_count = obj.batch.orders.count()

        result = {
            'id': str(obj.batch.id),
            'status': obj.batch.status,
            'batch_type': obj.batch.batch_type,
            'order_count': order_count,
            'route_distance_km': float(obj.batch.route_distance_km) if obj.batch.route_distance_km else None,
            'route_optimization': obj.batch.route_optimization,
        }

        # Role-gated pricing fields
        if role == 'admin':
            result['driver_payout'] = float(obj.batch.driver_payout) if obj.batch.driver_payout else None
            result['user_paid_total'] = float(obj.batch.user_paid_total) if obj.batch.user_paid_total else None
            result['company_profit'] = float(obj.batch.company_profit) if obj.batch.company_profit else None
        elif role == 'driver':
            result['driver_payout'] = float(obj.batch.driver_payout) if obj.batch.driver_payout else None
        elif role in ('vendor', 'customer'):
            result['user_paid_total'] = float(obj.batch.user_paid_total) if obj.batch.user_paid_total else None

        return result

    def get_estimated_arrival_minutes(self, obj):
        result = self._get_cached_eta(obj)
        return result.get('total_minutes')

    def get_waiting_info(self, obj):
        return self._get_cached_eta(obj)

    def _get_cached_eta(self, obj):
        if not hasattr(self, '_eta_cache'):
            self._eta_cache = {}
        order_id = str(obj.id)
        if order_id not in self._eta_cache:
            from domain.services.eta_service import ETAService
            self._eta_cache[order_id] = ETAService.calculate(obj)
        return self._eta_cache[order_id]

    def get_original_delivery_fee(self, obj):
        if obj.original_delivery_fee and obj.original_delivery_fee > 0:
            return float(obj.original_delivery_fee)
        return float(obj.delivery_fee)

    def get_discount_amount(self, obj):
        if obj.discount_applied and obj.original_delivery_fee and obj.original_delivery_fee > 0:
            return float(obj.original_delivery_fee - obj.delivery_fee)
        return 0.0

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['estimated_arrival_minutes'] = self.get_estimated_arrival_minutes(instance)
        data['waiting_info'] = self.get_waiting_info(instance)
        
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            user = request.user
            # Hide OTP from drivers (only customer should see it)
            if instance.customer != user:
                data.pop('delivery_otp', None)
            # Hide pricing details from non-admin roles
            if user.role != 'admin':
                data.pop('original_delivery_fee', None)
                data.pop('discount_applied', None)
                data.pop('discount_amount', None)
            
        return data

    def validate_items(self, value):
        if not value:
            raise serializers.ValidationError("Order must have at least one item.")
        for item in value:
            if item.get('quantity', 0) <= 0:
                raise serializers.ValidationError("Quantity must be strictly greater than zero.")
        return value

    def create(self, validated_data):
        # Extract delivery_location directly from initial_data to bypass DRF PointField stripping
        point = parse_coordinate_dict(self.initial_data, 'delivery_location')
        if point:
            validated_data['delivery_location'] = point
            
        from domain.services.order_services import PlaceOrderService
        return PlaceOrderService.execute(validated_data)


class DriverSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    is_platform = serializers.BooleanField(source='serves_all', read_only=True)
    recommended = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = ['id', 'user', 'status', 'location', 'latitude', 'longitude',
                  'rating', 'serves_all', 'is_platform', 'can_assign', 'priority',
                  'max_active_orders', 'current_active_orders', 'recommended']

    def get_latitude(self, obj):
        return obj.location.y if obj.location else None

    def get_longitude(self, obj):
        return obj.location.x if obj.location else None

    def get_recommended(self, obj):
        return str(obj.id) == self.context.get('recommended_id', '')



class DeliveryProofSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryProof
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'verified_at']

class DisputeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Dispute
        fields = '__all__'
        read_only_fields = [
            'id', 'raised_by', 'created_at', 'updated_at',
            'resolved_at', 'resolved_by', 'escalated_at',
        ]

class PoolItemSerializer(serializers.Serializer):
    """
    Serializes pool items stored as JSON on PoolMember.items.
    Each item is a dict: {food_id, name, quantity, price, image_url}
    The 'id' field is the food_id (used by the Flutter client as item identifier).
    """
    id = serializers.SerializerMethodField()
    food = serializers.CharField(source='food_id')
    food_name = serializers.CharField(source='name')
    quantity = serializers.IntegerField()
    price_at_addition = serializers.SerializerMethodField()

    def get_id(self, obj):
        # Flutter client uses food_id as the item identifier for remove-item calls
        return str(obj.get('food_id', ''))

    def get_price_at_addition(self, obj):
        # price in JSON is total (unit_price * qty); return unit price
        qty = obj.get('quantity', 1) or 1
        total = float(obj.get('price', 0))
        return round(total / qty, 2)


class PoolMemberSerializer(serializers.ModelSerializer):
    items = serializers.SerializerMethodField()
    username = serializers.ReadOnlyField(source='user.username')
    id = serializers.SerializerMethodField()

    class Meta:
        from apps.orders.models import PoolMember
        model = PoolMember
        fields = ['id', 'user', 'username', 'status', 'delivery_fee_share', 'service_fee_share', 'items_subtotal', 'items']

    def get_id(self, obj):
        return str(obj.id)

    def get_items(self, obj):
        # Items are stored as JSON on the member — no DB join needed
        items = obj.items if isinstance(obj.items, list) else []
        return PoolItemSerializer(items, many=True).data


class OrderPoolSerializer(serializers.ModelSerializer):
    def __init__(self, *args, **kwargs):
        # Allow dynamic field exclusion for performance (WebSockets)
        exclude_fields = kwargs.pop('exclude_fields', None)
        super(OrderPoolSerializer, self).__init__(*args, **kwargs)
        if exclude_fields:
            for field_name in exclude_fields:
                self.fields.pop(field_name, None)

    members = PoolMemberSerializer(many=True, read_only=True)
    restaurant_name = serializers.ReadOnlyField(source='restaurant.name')

    is_initiator = serializers.SerializerMethodField()
    current_user_member_id = serializers.SerializerMethodField()
    restaurant_menu = serializers.SerializerMethodField()
    
    class Meta:
        from apps.orders.models import OrderPool
        model = OrderPool
        fields = [
            'id', 'initiator', 'restaurant', 'restaurant_name', 'invite_code', 
            'status', 'delivery_address', 'delivery_location', 
            'total_delivery_fee', 'total_service_fee', 'members', 'created_at', 'expires_at',
            'is_initiator', 'current_user_member_id', 'restaurant_menu', 'final_order_id'
        ]
        read_only_fields = ['invite_code', 'status', 'total_delivery_fee', 'total_service_fee', 'initiator', 'expires_at']

    total_service_fee = serializers.ReadOnlyField(source='service_fee')

    final_order_id = serializers.SerializerMethodField()

    def get_final_order_id(self, obj):
        # Return the ID of the finalized order belonging to the current user if it exists
        request = self.context.get('request')
        if request and hasattr(request, 'user') and request.user.is_authenticated:
            order = obj.final_orders.filter(customer=request.user).first()
            if order:
                return str(order.id)
        # Fallback to the first order if request context is missing or order is not found
        order = obj.final_orders.first()
        return str(order.id) if order else None


    def get_restaurant_menu(self, obj):
        # Performance: Only query foods if this field wasn't excluded via exclude_fields
        # (it's excluded on all real-time WebSocket broadcasts — only sent on initial page load).
        if not obj.restaurant:
            return []
        # Use prefetched cache if available, otherwise fall back to filtered query
        if hasattr(obj.restaurant, '_prefetched_objects_cache') and 'sections' in obj.restaurant._prefetched_objects_cache:
            foods = []
            for section in obj.restaurant.sections.all():
                # section.foods is prefetched if sections__foods was prefetched
                foods.extend([f for f in section.foods.all() if f.is_available])
        else:
            from apps.products.models import Food
            foods = Food.objects.filter(section__restaurant=obj.restaurant, is_available=True).only('id', 'name', 'price', 'category', 'image')
        return [{'id': str(f.id), 'name': f.name, 'price': float(f.price), 'category': f.category, 'image': f.image.url if f.image else None} for f in foods]


    def get_is_initiator(self, obj):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            return obj.initiator_id == request.user.id
        return False

    def get_current_user_member_id(self, obj):
        request = self.context.get('request')
        if request and hasattr(request, 'user'):
            # Using .members.all() which is prefetched in the viewset
            for member in obj.members.all():
                if member.user_id == request.user.id:
                    return str(member.id)
        return None

    def create(self, validated_data):
        point = parse_coordinate_dict(self.initial_data, 'delivery_location')
        if point:
            validated_data['delivery_location'] = point
        return super().create(validated_data)

    def update(self, instance, validated_data):
        point = parse_coordinate_dict(self.initial_data, 'delivery_location')
        if point:
            validated_data['delivery_location'] = point
        return super().update(instance, validated_data)
