from rest_framework import serializers
from apps.geo.models import OperationalZone

class OperationalZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = OperationalZone
        fields = ['id', 'name', 'description', 'boundary', 'is_active', 'metadata']

class OperationalZoneMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = OperationalZone
        fields = ['id', 'name', 'description']
