from apps.orders.models import Order
from apps.reviews.models import Review
from rest_framework import serializers


class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = '__all__'
        read_only_fields = ('id', 'customer', 'created_at')

    def validate_rating(self, value):
        if value < 1 or value > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value

    def validate(self, data):
        review_type = data.get('review_type')

        if review_type == 'restaurant' and not data.get('restaurant'):
            raise serializers.ValidationError("Restaurant is required for a restaurant review.")
        if review_type == 'driver' and not data.get('driver'):
            raise serializers.ValidationError("Driver is required for a driver review.")
        if review_type == 'marketplace' and not data.get('marketplace_product'):
            raise serializers.ValidationError("Marketplace product is required for a marketplace review.")

        order_id = data.get('order')
        if order_id:
            try:
                order = Order.objects.get(id=order_id)
            except Order.DoesNotExist:
                raise serializers.ValidationError("Order not found.")
            if order.status != 'DELIVERED':
                raise serializers.ValidationError("Can only review a delivered order.")
            request = self.context.get('request')
            if request and order.customer_id != request.user.id:
                raise serializers.ValidationError("You can only review your own orders.")

        return data
