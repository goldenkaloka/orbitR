from api.serializers.pricing import (
    DeliveryFeeLogSerializer,
    PricingPolicySerializer,
    ZonePricingTierSerializer,
)
from apps.pricing.models import DeliveryFeeLog, PricingPolicy, ZonePricingTier
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response


class PricingPolicyViewSet(viewsets.ModelViewSet):
    queryset = PricingPolicy.objects.all().order_by('-created_at')
    serializer_class = PricingPolicySerializer
    permission_classes = [IsAdminUser]

    @action(detail=True, methods=['POST'])
    def activate(self, request, pk=None):
        policy = self.get_object()
        policy.is_active = True
        policy.save()
        return Response({'status': 'policy activated'})

class ZonePricingTierViewSet(viewsets.ModelViewSet):
    queryset = ZonePricingTier.objects.all()
    serializer_class = ZonePricingTierSerializer
    permission_classes = [IsAdminUser]

class DeliveryFeeLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DeliveryFeeLog.objects.all().order_by('-calculated_at')
    serializer_class = DeliveryFeeLogSerializer
    permission_classes = [IsAdminUser]
