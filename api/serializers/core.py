from decimal import Decimal
from rest_framework import serializers

from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.products.models import Food
from apps.orders.models import Order, OrderItem, Dispute
from apps.delivery.models import Driver, DeliveryProof
from apps.core.models import Notification
from domain.events.bus import EventBus
from domain.events.types import OrderCreated
from api.serializers.utils import parse_coordinate_dict


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'title', 'body', 'data', 'is_read', 'created_at']


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'phone', 'role']

class FoodSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    vendor_id = serializers.CharField(source='vendor.id', read_only=True)
    vendor_name = serializers.CharField(source='vendor.name', read_only=True)

    class Meta:
        model = Food
        fields = ['id', 'name', 'description', 'price', 'category', 'is_available', 'image', 'image_url', 'vendor_id', 'vendor_name']

    def get_image_url(self, obj):
        image = getattr(obj, 'image', None) if not isinstance(obj, dict) else obj.get('image')
        if image:
            try:
                return image.url
            except (AttributeError, ValueError):
                return None
        return None



class RestaurantListSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()
    foods = FoodSerializer(many=True, read_only=True)

    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()

    class Meta:
        model = Restaurant
        fields = [
            'id', 'name', 'description', 'image', 'image_url', 
            'location', 'geo_location', 'latitude', 'longitude', 'category', 'price_range', 'rating', 
            'is_active', 'foods'
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


class RestaurantSerializer(RestaurantListSerializer):
    foods = FoodSerializer(many=True, read_only=True)

    class Meta(RestaurantListSerializer.Meta):
        fields = RestaurantListSerializer.Meta.fields + ['foods']
class OrderItemSerializer(serializers.ModelSerializer):
    food_name = serializers.SerializerMethodField()
    class Meta:
        model = OrderItem
        fields = ['food', 'food_name', 'quantity', 'price']
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
    
    rider_name = serializers.ReadOnlyField(source='driver.user.username')
    rider_phone = serializers.ReadOnlyField(source='driver.user.phone')
    customer_name = serializers.ReadOnlyField(source='customer.username')
    customer_phone = serializers.ReadOnlyField(source='customer.phone')
    
    batch = serializers.SerializerMethodField()
    original_delivery_fee = serializers.SerializerMethodField()
    discount_amount = serializers.SerializerMethodField()
    
    class Meta:
        model = Order
        fields = [
            'id', 'status', 'is_paid', 'items_subtotal', 'delivery_fee', 'service_fee', 'total_amount',
            'original_delivery_fee', 'discount_applied', 'discount_amount',
            'restaurant', 'restaurant_name', 'restaurant_latitude', 'restaurant_longitude',
            'customer_name', 'customer_phone',
            'items', 'delivery_address', 'delivery_location', 'delivery_latitude', 'delivery_longitude',
            'notes', 'rider_name', 'rider_phone', 'created_at', 'delivery_otp', 'group_breakdown',
            'batch'
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


    def get_restaurant_name(self, obj):
        return obj.restaurant.name if obj.restaurant else "Marketplace Order"

    def get_batch(self, obj):
        if not obj.batch:
            return None
        return {
            'id': str(obj.batch.id),
            'status': obj.batch.status,
            'batch_type': obj.batch.batch_type,
            'order_count': obj.batch.orders.count(),
            'driver_payout': float(obj.batch.driver_payout) if obj.batch.driver_payout else None,
            'route_distance_km': float(obj.batch.route_distance_km) if obj.batch.route_distance_km else None,
            'route_optimization': obj.batch.route_optimization,
            'user_paid_total': float(obj.batch.user_paid_total) if obj.batch.user_paid_total else None,
            'company_profit': float(obj.batch.company_profit) if obj.batch.company_profit else None,
        }

    def get_estimated_arrival_minutes(self, obj):
        if not obj.restaurant or not obj.delivery_location or not obj.restaurant.geo_location:
            return 30

        try:
            dist_km = obj.restaurant.geo_location.distance(obj.delivery_location) / 1000
            est = 8 + (dist_km * 4)
            return int(max(15, min(60, est)))
        except Exception:
            return 30

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
        
        # Hide OTP from drivers (only customer should see it)
        request = self.context.get('request')
        if request and hasattr(request, 'user') and instance.customer != request.user:
            data.pop('delivery_otp', None)
            
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

    class Meta:
        model = Driver
        fields = ['id', 'user', 'status', 'location', 'latitude', 'longitude', 'rating']

    def get_latitude(self, obj):
        return obj.location.y if obj.location else None

    def get_longitude(self, obj):
        return obj.location.x if obj.location else None



class DeliveryProofSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryProof
        fields = '__all__'
        read_only_fields = ['id', 'created_at', 'verified_at']

class DisputeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Dispute
        fields = '__all__'
        read_only_fields = ['id', 'raised_by', 'created_at', 'updated_at']

class PoolItemSerializer(serializers.ModelSerializer):
    food_name = serializers.ReadOnlyField(source='food.name')
    id = serializers.SerializerMethodField()
    class Meta:
        from apps.orders.models import PoolItem
        model = PoolItem
        fields = ['id', 'food', 'food_name', 'quantity', 'price_at_addition']

    def get_id(self, obj):
        return str(obj.id)


class PoolMemberSerializer(serializers.ModelSerializer):
    items = PoolItemSerializer(many=True, read_only=True)
    username = serializers.ReadOnlyField(source='user.username')
    id = serializers.SerializerMethodField()
    class Meta:
        from apps.orders.models import PoolMember
        model = PoolMember
        fields = ['id', 'user', 'username', 'status', 'delivery_fee_share', 'service_fee_share', 'items_subtotal', 'items']

    def get_id(self, obj):
        return str(obj.id)


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
            'total_delivery_fee', 'service_fee', 'members', 'created_at', 'expires_at',
            'is_initiator', 'current_user_member_id', 'restaurant_menu', 'final_order_id'
        ]
        read_only_fields = ['invite_code', 'status', 'total_delivery_fee', 'initiator', 'expires_at']

    final_order_id = serializers.SerializerMethodField()

    def get_final_order_id(self, obj):
        # Return the ID of the finalized order if it exists
        order = obj.final_orders.order_by('-created_at').first()
        return str(order.id) if order else None


    def get_restaurant_menu(self, obj):
        # Performance: Only query foods if this field wasn't excluded via exclude_fields
        # (it's excluded on all real-time WebSocket broadcasts — only sent on initial page load).
        if not obj.restaurant:
            return []
        # Use prefetched cache if available, otherwise fall back to filtered query
        if hasattr(obj.restaurant, '_prefetched_objects_cache') and 'foods' in obj.restaurant._prefetched_objects_cache:
            foods = [f for f in obj.restaurant.foods.all() if f.is_available]
        else:
            foods = obj.restaurant.foods.filter(is_available=True).only('id', 'name', 'price', 'category', 'image')
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
