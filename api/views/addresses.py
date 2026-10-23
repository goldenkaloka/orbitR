from api.serializers.addresses import (
    MAX_ADDRESSES_PER_USER,
    UserAddressSerializer,
)
from apps.users.models import UserAddress
from django.db import transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


class UserAddressViewSet(viewsets.ModelViewSet):
    """CRUD for the signed-in customer's saved delivery addresses."""

    serializer_class = UserAddressSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_queryset(self):
        return UserAddress.objects.filter(
            user=self.request.user
        ).select_related('zone')

    def perform_create(self, serializer):
        existing = UserAddress.objects.filter(user=self.request.user).count()
        if existing >= MAX_ADDRESSES_PER_USER:
            raise ValidationError(
                f'You can save up to {MAX_ADDRESSES_PER_USER} addresses. '
                'Delete one before adding another.'
            )
        serializer.save(user=self.request.user)

    def perform_destroy(self, instance):
        was_default = instance.is_default
        user = instance.user
        instance.delete()
        if was_default:
            with transaction.atomic():
                replacement = (
                    UserAddress.objects.filter(user=user)
                    .order_by('-last_used_at', '-created_at')
                    .first()
                )
                if replacement is not None:
                    UserAddress.objects.filter(pk=replacement.pk).update(
                        is_default=True
                    )

    @action(detail=True, methods=['post'], url_path='set-default')
    def set_default(self, request, pk=None):
        address = self.get_object()
        with transaction.atomic():
            UserAddress.objects.filter(user=request.user, is_default=True).update(
                is_default=False
            )
            UserAddress.objects.filter(pk=address.pk).update(is_default=True)
        address.refresh_from_db()
        return Response(self.get_serializer(address).data)

    @action(detail=True, methods=['post'])
    def use(self, request, pk=None):
        """Record that an address was used at checkout.

        Used to rank the address list by habit and, later, to feed anonymous
        place-name learning.
        """
        address = self.get_object()
        UserAddress.objects.filter(pk=address.pk).update(
            use_count=address.use_count + 1,
            last_used_at=timezone.now(),
        )
        address.refresh_from_db()
        return Response(self.get_serializer(address).data, status=status.HTTP_200_OK)
