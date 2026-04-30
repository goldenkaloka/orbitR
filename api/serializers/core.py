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

    class Meta:
        model = Food
        fields = ['id', 'name', 'description', 'price', 'is_available', 'image', 'image_url']

    def get_image_url(self, obj):
        # Handle both dict (from validated_data) and model instance
        image = getattr(obj, 'image', None) if not isinstance(obj, dict) else obj.get('image')
        if image:
            try:
                return image.url
            except (AttributeError, ValueError):
                return None
        return None

class RestaurantSerializer(serializers.ModelSerializer):
    foods = FoodSerializer(many=True, read_only=True)
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = Restaurant
        fields = [
            'id', 'name', 'description', 'image', 'image_url', 
            'location', 'geo_location', 'latitude', 'longitude', 'category', 'price_range', 'rating', 
            'is_active', 'foods'
        ]

    def get_image_url(self, obj):
        image = getattr(obj, 'image', None) if not isinstance(obj, dict) else obj.get('image')
        if image:
            try:
                return image.url
            except (AttributeError, ValueError):
                return None
        return None

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
    restaurant_latitude = serializers.ReadOnlyField(source='restaurant.latitude')
    restaurant_longitude = serializers.ReadOnlyField(source='restaurant.longitude')
    rider_name = serializers.ReadOnlyField(source='driver.user.username')
    rider_phone = serializers.ReadOnlyField(source='driver.user.phone')
    customer_name = serializers.ReadOnlyField(source='customer.username')
    customer_phone = serializers.ReadOnlyField(source='customer.phone')
    
    class Meta:
        model = Order
        fields = [
            'id', 'status', 'items_subtotal', 'delivery_fee', 'total_amount', 
            'restaurant', 'restaurant_name', 'restaurant_latitude', 'restaurant_longitude',
            'customer_name', 'customer_phone',
            'items', 'delivery_address', 'delivery_location', 'delivery_latitude', 'delivery_longitude', 
            'notes', 'rider_name', 'rider_phone', 'created_at', 'delivery_otp'
        ]
        read_only_fields = ['status', 'items_subtotal', 'delivery_fee', 'total_amount', 'delivery_otp']

    def get_restaurant_name(self, obj):
        return obj.restaurant.name if obj.restaurant else "Marketplace Order"

    def get_estimated_arrival_minutes(self, obj):
        if not obj.restaurant or not obj.delivery_location:
            return 30
        return 25 

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
        from domain.services.order_services import PlaceOrderService
        return PlaceOrderService.execute(validated_data)



class DriverSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    class Meta:
        model = Driver
        fields = ['id', 'user', 'status', 'location', 'latitude', 'longitude', 'rating']

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
    class Meta:
        model = OrderItem # Note: reusing OrderItem model for convenience in serializer if possible, or use PoolItem
        from apps.orders.models import PoolItem
        model = PoolItem
        fields = ['id', 'food', 'food_name', 'quantity', 'price_at_addition']

class PoolMemberSerializer(serializers.ModelSerializer):
    items = PoolItemSerializer(many=True, read_only=True)
    username = serializers.ReadOnlyField(source='user.username')
    class Meta:
        from apps.orders.models import PoolMember
        model = PoolMember
        fields = ['id', 'user', 'username', 'status', 'delivery_fee_share', 'items_subtotal', 'items']

class OrderPoolSerializer(serializers.ModelSerializer):
    members = PoolMemberSerializer(many=True, read_only=True)
    restaurant_name = serializers.ReadOnlyField(source='restaurant.name')
    
    class Meta:
        from apps.orders.models import OrderPool
        model = OrderPool
        fields = [
            'id', 'initiator', 'restaurant', 'restaurant_name', 'invite_code', 
            'status', 'delivery_address', 'delivery_location', 
            'total_delivery_fee', 'members', 'created_at', 'expires_at'
        ]
        read_only_fields = ['invite_code', 'status', 'total_delivery_fee', 'initiator']
