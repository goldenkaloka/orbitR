from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from django.db import transaction

from apps.restaurants.models import RestaurantSection, SectionStaff
from apps.orders.models import BatchSectionStatus
from api.serializers.core import RestaurantSectionSerializer, BatchSectionStatusSerializer


class SectionViewSet(viewsets.ModelViewSet):
    serializer_class = RestaurantSectionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        owned = RestaurantSection.objects.filter(restaurant__owner=user)
        managed = RestaurantSection.objects.filter(staff__user=user)
        return (owned | managed).distinct()

    def perform_create(self, serializer):
        restaurant_id = self.request.data.get('restaurant')
        from apps.restaurants.models import Restaurant
        restaurant = Restaurant.objects.get(id=restaurant_id)
        if restaurant.owner != self.request.user:
            raise PermissionDenied('Only the restaurant owner can create sections')
        serializer.save(restaurant=restaurant)

    def perform_update(self, serializer):
        section = self.get_object()
        if section.restaurant.owner != self.request.user:
            raise PermissionDenied('Only the restaurant owner can edit sections')
        serializer.save()


class BatchSectionStatusViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = BatchSectionStatusSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        return BatchSectionStatus.objects.filter(
            section__restaurant__owner=user
        ) | BatchSectionStatus.objects.filter(
            section__staff__user=user
        )

    @action(detail=True, methods=['post'])
    def mark_ready(self, request, pk=None):
        """Stall manager marks their section's items in a batch as READY."""
        bss = self.get_object()
        user = request.user

        is_owner = bss.section.restaurant.owner == user
        is_staff = SectionStaff.objects.filter(user=user, section=bss.section).exists()
        if not is_owner and not is_staff:
            return Response({'error': 'Not authorized for this section'}, status=403)

        if bss.status == 'READY':
            return Response({'error': 'Already marked READY'}, status=400)

        with transaction.atomic():
            bss.status = 'READY'
            bss.save(update_fields=['status'])

            batch = bss.batch
            all_ready = not batch.section_statuses.exclude(status='READY').exists()
            payload = {
                'section_status': BatchSectionStatusSerializer(bss).data,
                'batch_all_ready': all_ready,
            }

        return Response(payload)
