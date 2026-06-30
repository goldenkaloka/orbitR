from apps.orders.models import Order
from apps.reviews.models import Review
from rest_framework import serializers


class ReviewSerializer(serializers.ModelSerializer):
    customer_name = serializers.SerializerMethodField()

    class Meta:
        model = Review
        fields = '__all__'
        read_only_fields = ('id', 'customer', 'created_at')

    def get_customer_name(self, obj):
        username = getattr(obj.customer, 'username', '') or ''
        if username:
            return username
        phone = getattr(obj.customer, 'phone', '') or ''
        if phone and len(phone) >= 4:
            return f"Customer •••{phone[-4:]}"
        return 'Customer'

    def validate_rating(self, value):
        if value < 1 or value > 5:
            raise serializers.ValidationError("Rating must be between 1 and 5.")
        return value

    def validate(self, data):
        review_type = data.get('review_type') or getattr(self.instance, 'review_type', None)
        request = self.context.get('request')

        if self.instance:
            immutable_fields = {'review_type', 'order', 'restaurant', 'driver', 'marketplace_product'}
            attempted = immutable_fields.intersection(self.initial_data.keys())
            if attempted:
                raise serializers.ValidationError(
                    f"Review target fields cannot be changed after creation: {', '.join(sorted(attempted))}."
                )

        restaurant = data.get('restaurant') or getattr(self.instance, 'restaurant', None)
        driver = data.get('driver') or getattr(self.instance, 'driver', None)
        marketplace_product = data.get('marketplace_product') or getattr(self.instance, 'marketplace_product', None)

        if review_type == 'restaurant' and not restaurant:
            raise serializers.ValidationError("Restaurant is required for a restaurant review.")
        if review_type == 'driver' and not driver:
            raise serializers.ValidationError("Driver is required for a driver review.")
        if review_type == 'marketplace' and not marketplace_product:
            raise serializers.ValidationError("Marketplace product is required for a marketplace review.")
        if review_type == 'restaurant' and (driver or marketplace_product):
            raise serializers.ValidationError("Restaurant reviews cannot include driver or marketplace targets.")
        if review_type == 'driver' and (restaurant or marketplace_product):
            raise serializers.ValidationError("Driver reviews cannot include restaurant or marketplace targets.")
        if review_type == 'marketplace' and (restaurant or driver):
            raise serializers.ValidationError("Marketplace reviews cannot include restaurant or driver targets.")

        order = data.get('order')
        if order:
            try:
                order = Order.objects.select_related('restaurant', 'driver__user').get(id=order.id)
            except Order.DoesNotExist:
                raise serializers.ValidationError("Order not found.")
            if order.status != 'DELIVERED':
                raise serializers.ValidationError("Can only review a delivered order.")
            if request and order.customer_id != request.user.id:
                raise serializers.ValidationError("You can only review your own orders.")

            if review_type == 'restaurant' and restaurant and order.restaurant_id != restaurant.id:
                raise serializers.ValidationError("Restaurant review target must match the delivered order.")
            if review_type == 'driver':
                if not order.driver_id:
                    raise serializers.ValidationError("This order has no assigned driver to review.")
                if driver and order.driver_id != driver.id:
                    raise serializers.ValidationError("Driver review target must match the delivered order.")
            if review_type == 'marketplace':
                raise serializers.ValidationError("Marketplace reviews are not tied to food delivery orders.")
        elif review_type in ('restaurant', 'driver'):
            raise serializers.ValidationError("Delivered order is required for restaurant and driver reviews.")

        if review_type == 'marketplace':
            if not marketplace_product:
                raise serializers.ValidationError("Marketplace product is required for a marketplace review.")
            if request and marketplace_product.seller_id == request.user.id:
                raise serializers.ValidationError("You cannot review your own marketplace listing.")
            raise serializers.ValidationError(
                "Marketplace reviews require buyer-side purchase records before they can be accepted."
            )

        return data

    def create(self, validated_data):
        review_type = validated_data.get('review_type')
        validated_data['is_verified_purchase'] = review_type in ('restaurant', 'driver', 'marketplace')
        comment = (validated_data.get('comment') or '').strip()
        if _requires_moderation(comment):
            validated_data['is_visible'] = False
            validated_data['moderation_status'] = 'PENDING'
        return super().create(validated_data)

    def update(self, instance, validated_data):
        comment = (validated_data.get('comment', instance.comment) or '').strip()
        if _requires_moderation(comment):
            validated_data['is_visible'] = False
            validated_data['moderation_status'] = 'PENDING'
        return super().update(instance, validated_data)


def _requires_moderation(comment):
    if not comment:
        return False
    lowered = comment.lower()
    blocked_terms = ('http://', 'https://', 'www.', '@', 'telegram', 'whatsapp')
    return any(term in lowered for term in blocked_terms)
