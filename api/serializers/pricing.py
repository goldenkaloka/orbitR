from rest_framework import serializers
from apps.pricing.models import PricingPolicy, ZonePricingTier, DeliveryFeeLog

class PricingPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = PricingPolicy
        fields = '__all__'

class ZonePricingTierSerializer(serializers.ModelSerializer):
    zone_name = serializers.ReadOnlyField(source='zone.name')
    class Meta:
        model = ZonePricingTier
        fields = ['id', 'zone', 'zone_name', 'per_km_multiplier', 'base_fee_override']

class DeliveryFeeLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeliveryFeeLog
        fields = '__all__'
