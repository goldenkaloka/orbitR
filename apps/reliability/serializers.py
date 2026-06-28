from rest_framework import serializers

from .models import ReliabilityRecord, Strike


class StrikeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Strike
        fields = ['id', 'reason', 'weight', 'waived', 'created_at', 'expires_at']


class ReliabilitySerializer(serializers.ModelSerializer):
    strikes = StrikeSerializer(many=True, read_only=True)
    level = serializers.SerializerMethodField()
    level_label = serializers.SerializerMethodField()

    class Meta:
        model = ReliabilityRecord
        fields = ['id', 'role', 'score', 'level', 'level_label', 'strikes', 'updated_at']

    def get_level(self, obj):
        from .services import get_threshold
        return get_threshold(obj.score)[1]

    def get_level_label(self, obj):
        from .services import get_threshold
        return get_threshold(obj.score)[2]
