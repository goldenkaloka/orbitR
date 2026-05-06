from rest_framework import serializers
from apps.geo.models import OperationalZone

class OperationalZoneSerializer(serializers.ModelSerializer):
    h3_count = serializers.IntegerField(source='h3_indexes.count', read_only=True)

    class Meta:
        model = OperationalZone
        fields = ['id', 'name', 'description', 'h3_count', 'is_active']

class OperationalZoneMinimalSerializer(serializers.ModelSerializer):
    class Meta:
        model = OperationalZone
        fields = ['id', 'name', 'description']
