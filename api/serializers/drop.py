"""Serializers for scheduled drop points and windows."""

from django.contrib.gis.geos import Point
from rest_framework import serializers

from apps.orders.models import DropPoint, DropWindow
from domain.services.batch_pipeline import MAX_ORDERS_PER_BATCH


def _check_trip_size(value):
    """More than one batch means a second trip paying the flat fee again."""
    if value is not None and value > MAX_ORDERS_PER_BATCH:
        raise serializers.ValidationError(
            f'A drop trip carries at most {MAX_ORDERS_PER_BATCH} orders; '
            'a larger window would open a second trip.'
        )
    return value


def _point_from_payload(latitude, longitude, raw):
    """Build a Point from either lat/lng fields or a GeoJSON dict."""
    if latitude is not None and longitude is not None:
        return Point(float(longitude), float(latitude))
    if isinstance(raw, dict):
        if 'coordinates' in raw:
            lng, lat = raw['coordinates'][:2]
            return Point(float(lng), float(lat))
        if 'longitude' in raw and 'latitude' in raw:
            return Point(float(raw['longitude']), float(raw['latitude']))
        if 'lng' in raw and 'lat' in raw:
            return Point(float(raw['lng']), float(raw['lat']))
    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
        return Point(float(raw[0]), float(raw[1]))
    return None


class DropWindowSerializer(serializers.ModelSerializer):
    drop_point_name = serializers.ReadOnlyField(source='drop_point.name')
    weekday_label = serializers.SerializerMethodField()
    start_label = serializers.SerializerMethodField()
    end_label = serializers.SerializerMethodField()
    cutoff_label = serializers.SerializerMethodField()

    class Meta:
        model = DropWindow
        fields = [
            'id', 'drop_point', 'drop_point_name', 'label', 'weekday',
            'weekday_label', 'start_time', 'end_time', 'cutoff_time',
            'start_label', 'end_label', 'cutoff_label',
            'delivery_fee', 'max_orders', 'is_active',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_weekday_label(self, obj):
        return 'Every day' if obj.weekday is None else obj.get_weekday_display()

    def get_start_label(self, obj):
        return obj.start_time.strftime('%H:%M') if obj.start_time else None

    def get_end_label(self, obj):
        return obj.end_time.strftime('%H:%M') if obj.end_time else None

    def get_cutoff_label(self, obj):
        return obj.cutoff_time.strftime('%H:%M') if obj.cutoff_time else None

    def validate_max_orders(self, value):
        return _check_trip_size(value)

    def validate(self, attrs):
        start = attrs.get('start_time', getattr(self.instance, 'start_time', None))
        end = attrs.get('end_time', getattr(self.instance, 'end_time', None))
        cutoff = attrs.get('cutoff_time', getattr(self.instance, 'cutoff_time', None))
        if start and end and end <= start:
            raise serializers.ValidationError(
                {'end_time': 'The hand-over must end after it starts.'}
            )
        if start and cutoff and cutoff > start:
            raise serializers.ValidationError(
                {'cutoff_time': 'Orders must close before the hand-over opens.'}
            )
        return attrs


class DropPointSerializer(serializers.ModelSerializer):
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    windows = DropWindowSerializer(many=True, read_only=True)
    # Accept lat/lng (or a GeoJSON point) on write; output uses latitude/longitude.
    geo_location = serializers.JSONField(required=False, write_only=True)
    write_latitude = serializers.FloatField(required=False, write_only=True)
    write_longitude = serializers.FloatField(required=False, write_only=True)

    class Meta:
        model = DropPoint
        fields = [
            'id', 'name', 'description', 'geo_location',
            'write_latitude', 'write_longitude', 'latitude', 'longitude',
            'h3_index', 'zone', 'min_orders', 'max_orders',
            'contact_name', 'contact_phone', 'notes', 'is_active',
            'windows', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'h3_index', 'created_at', 'updated_at']

    def get_latitude(self, obj):
        return obj.geo_location.y if obj.geo_location else None

    def get_longitude(self, obj):
        return obj.geo_location.x if obj.geo_location else None

    def validate_max_orders(self, value):
        return _check_trip_size(value)

    def validate(self, attrs):
        minimum = attrs.get('min_orders', getattr(self.instance, 'min_orders', None))
        maximum = attrs.get('max_orders', getattr(self.instance, 'max_orders', None))
        if minimum and maximum and minimum > maximum:
            raise serializers.ValidationError(
                {'min_orders': 'The minimum cannot exceed the trip capacity.'}
            )
        return attrs

    def _apply_point(self, validated_data):
        latitude = validated_data.pop('write_latitude', None)
        longitude = validated_data.pop('write_longitude', None)
        raw = validated_data.pop('geo_location', None)
        point = _point_from_payload(latitude, longitude, raw)
        if point is not None:
            validated_data['geo_location'] = point
        return validated_data

    def create(self, validated_data):
        validated_data = self._apply_point(validated_data)
        if not validated_data.get('geo_location'):
            raise serializers.ValidationError(
                {'geo_location': 'A drop point needs a latitude and longitude.'}
            )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        return super().update(instance, self._apply_point(validated_data))

