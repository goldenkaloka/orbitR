from rest_framework import serializers
from apps.restaurants.models import OperationalZone

class OperationalZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = OperationalZone
        fields = ['id', 'name', 'description', 'h3_index', 'is_active']

class OperationalZoneMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = OperationalZone
        fields = ['id', 'name', 'description']
