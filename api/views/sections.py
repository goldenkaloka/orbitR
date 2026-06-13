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

    @action(detail=True, methods=['post'], url_path='add-staff')
    def add_staff(self, request, pk=None):
        """
        Add a user as section staff.
        Body: { user_id }
        Only the restaurant owner can call this.
        """
        section = self.get_object()
        if section.restaurant.owner != request.user:
            return Response({'error': 'Only the restaurant owner can manage staff'}, status=403)

        user_id = request.data.get('user_id')
        if not user_id:
            return Response({'error': 'user_id is required'}, status=400)

        from apps.users.models import User as AppUser
        try:
            target_user = AppUser.objects.get(id=user_id)
        except AppUser.DoesNotExist:
            return Response({'error': 'User not found'}, status=404)

        staff, created = SectionStaff.objects.get_or_create(
            user=target_user,
            section=section,
        )
        return Response({
            'id': str(staff.id),
            'user_id': str(target_user.id),
            'username': target_user.username,
            'section_id': str(section.id),
            'section_name': section.name,
            'created': created,
        }, status=201 if created else 200)

    @action(detail=True, methods=['post'], url_path='remove-staff')
    def remove_staff(self, request, pk=None):
        """
        Remove a user from section staff.
        Body: { user_id }
        Only the restaurant owner can call this.
        """
        section = self.get_object()
        if section.restaurant.owner != request.user:
            return Response({'error': 'Only the restaurant owner can manage staff'}, status=403)

        user_id = request.data.get('user_id')
        if not user_id:
            return Response({'error': 'user_id is required'}, status=400)

        deleted, _ = SectionStaff.objects.filter(
            user_id=user_id,
            section=section,
        ).delete()

        if deleted == 0:
            return Response({'error': 'This user is not staff for this section'}, status=404)

        return Response({'status': 'staff removed'})

    @action(detail=True, methods=['get'], url_path='staff')
    def list_staff(self, request, pk=None):
        """
        List all staff members for a section.
        Accessible to the restaurant owner only.
        """
        section = self.get_object()
        if section.restaurant.owner != request.user:
            return Response({'error': 'Only the restaurant owner can view staff'}, status=403)

        staff_members = SectionStaff.objects.filter(section=section).select_related('user')
        return Response([
            {
                'staff_id': str(s.id),
                'user_id': str(s.user.id),
                'username': s.user.username,
                'phone': s.user.phone,
            }
            for s in staff_members
        ])


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
