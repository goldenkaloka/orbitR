from api.serializers.utils import parse_coordinate_dict
from apps.users.models import UserAddress
from django.db import transaction
from rest_framework import serializers

MAX_ADDRESSES_PER_USER = 20


class UserAddressSerializer(serializers.ModelSerializer):
    latitude = serializers.SerializerMethodField()
    longitude = serializers.SerializerMethodField()
    zone_name = serializers.SerializerMethodField()

    class Meta:
        model = UserAddress
        fields = [
            'id', 'label', 'place_name', 'address_line', 'landmark',
            'latitude', 'longitude', 'zone_name', 'is_default',
            'use_count', 'last_used_at', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'use_count', 'last_used_at', 'created_at', 'updated_at',
        ]

    def get_latitude(self, obj):
        return obj.delivery_location.y if obj.delivery_location else None

    def get_longitude(self, obj):
        return obj.delivery_location.x if obj.delivery_location else None

    def get_zone_name(self, obj):
        return obj.zone.name if obj.zone else None

    def validate_label(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('A label is required.')
        if len(value) > 40:
            raise serializers.ValidationError('Keep the label under 40 characters.')

        user = getattr(self.context.get('request'), 'user', None)
        if user is not None:
            existing = UserAddress.objects.filter(user=user, label__iexact=value)
            if self.instance is not None:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise serializers.ValidationError(
                    'You already have an address with this label.'
                )
        return value

    def validate_place_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError(
                'Tell us what people call this place (e.g. "Kwa Mangi").'
            )
        if len(value) > 120:
            raise serializers.ValidationError(
                'Keep the place name under 120 characters.'
            )
        return value

    def validate(self, attrs):
        point = parse_coordinate_dict(self.initial_data or {}, 'delivery_location')
        if point is None and self.instance is None:
            raise serializers.ValidationError(
                {'latitude': 'Pick a location on the map or use your GPS.'}
            )
        return attrs

    def create(self, validated_data):
        point = parse_coordinate_dict(self.initial_data or {}, 'delivery_location')
        if point is not None:
            validated_data['delivery_location'] = point
            validated_data['zone'] = None

        user = validated_data['user']
        has_addresses = UserAddress.objects.filter(user=user).exists()
        if not has_addresses or validated_data.get('is_default'):
            validated_data['is_default'] = True

        address = super().create(validated_data)
        if address.is_default:
            self._clear_other_defaults(address)
        self._learn_place_name(address)
        return address

    def update(self, instance, validated_data):
        point = parse_coordinate_dict(self.initial_data or {}, 'delivery_location')
        if point is not None:
            validated_data['delivery_location'] = point
            validated_data['zone'] = None

        address = super().update(instance, validated_data)
        if address.is_default:
            self._clear_other_defaults(address)
        self._learn_place_name(address)
        return address

    def _learn_place_name(self, address):
        from apps.geo.place_aliases import trigger_address_alias_learning

        transaction.on_commit(
            lambda: trigger_address_alias_learning(address.id)
        )

    def _clear_other_defaults(self, address):
        UserAddress.objects.filter(
            user=address.user, is_default=True
        ).exclude(pk=address.pk).update(is_default=False)
