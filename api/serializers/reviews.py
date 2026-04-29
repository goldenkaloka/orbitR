from rest_framework import serializers
from apps.reviews.models import Review

class ReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = Review
        fields = '__all__'
        read_only_fields = ('id', 'customer', 'created_at')

    def validate(self, data):
        """
        Custom validation to ensure correct target is provided for the review type.
        """
        review_type = data.get('review_type')
        if review_type == 'restaurant' and not data.get('restaurant'):
            raise serializers.ValidationError("Restaurant is required for a restaurant review.")
        if review_type == 'driver' and not data.get('driver'):
            raise serializers.ValidationError("Driver is required for a driver review.")
        return data
