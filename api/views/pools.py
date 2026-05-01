from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from apps.orders.models import OrderPool
from api.serializers.core import OrderPoolSerializer, PoolMemberSerializer
from domain.services.order_services import PoolOrderService

class PoolOrderViewSet(viewsets.ModelViewSet):
    queryset = OrderPool.objects.all()
    serializer_class = OrderPoolSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        # Users see pools they initiated or joined
        return OrderPool.objects.filter(
            models.Q(initiator=user) | models.Q(members__user=user)
        ).distinct().prefetch_related('members', 'members__user', 'members__items', 'members__items__food')

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            print(f"DEBUG: Pool Validation Errors: {serializer.errors}")
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_create(self, serializer):
        # The service handles creation with initiator logic
        restaurant_id = self.request.data.get('restaurant') or self.request.data.get('restaurant_id') or self.request.data.get('restaurantId')
        
        pool = PoolOrderService.create_pool(
            initiator=self.request.user,
            restaurant_id=restaurant_id,
            delivery_address=self.request.data.get('delivery_address'),
            delivery_location=self.request.data.get('delivery_location')
        )
        # We manually set the instance for the response
        serializer.instance = pool

    @action(detail=False, methods=['POST'], url_path='join')
    def join(self, request):
        invite_code = request.data.get('invite_code')
        if not invite_code:
            return Response({'error': 'Invite code is required'}, status=400)
        
        try:
            pool = PoolOrderService.join_pool(request.user, invite_code)
            return Response(OrderPoolSerializer(pool).data)
        except OrderPool.DoesNotExist:
            return Response({'error': 'Pool not found or closed'}, status=404)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='add-item')
    def add_item(self, request, pk=None):
        food_id = request.data.get('food_id')
        quantity = request.data.get('quantity', 1)
        
        try:
            pool = PoolOrderService.add_item(request.user, pk, food_id, quantity)
            return Response(OrderPoolSerializer(pool).data)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='lock')
    def lock(self, request, pk=None):
        pool = self.get_object()
        if pool.initiator != request.user:
            return Response({'error': 'Only the initiator can lock the pool'}, status=403)
        
        try:
            pool = PoolOrderService.lock_pool(pk)
            return Response(OrderPoolSerializer(pool).data)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='pay')
    def pay_share(self, request, pk=None):
        try:
            pool = PoolOrderService.pay_share(request.user, pk)
            return Response(OrderPoolSerializer(pool).data)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

# Import models for the Q query
from django.db import models
