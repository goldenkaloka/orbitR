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
        # UX Improvement: If user already has an open pool, just return it
        # instead of failing with a 400/500 error.
        existing_pool = OrderPool.objects.filter(initiator=request.user, status='OPEN').first()
        if existing_pool:
            serializer = self.get_serializer(existing_pool, context={'request': request})
            return Response(serializer.data, status=status.HTTP_200_OK)

        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            self.perform_create(serializer)
            headers = self.get_success_headers(serializer.data)
            return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({'error': 'An unexpected error occurred'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)



    def perform_create(self, serializer):
        # The service handles creation with initiator logic
        # We use validated_data which contains the parsed Point object and IDs
        restaurant = serializer.validated_data.get('restaurant')
        restaurant_id = getattr(restaurant, 'id', restaurant)
        
        pool = PoolOrderService.create_pool(
            initiator=self.request.user,
            restaurant_id=restaurant_id,
            delivery_address=serializer.validated_data.get('delivery_address'),
            delivery_location=serializer.validated_data.get('delivery_location')
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
            return Response(OrderPoolSerializer(pool, context={'request': request}).data)
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
            return Response(OrderPoolSerializer(pool, context={'request': request}).data)
        except Exception as e:
            return Response({'error': str(e)}, status=400)
    @action(detail=True, methods=['POST'], url_path='remove-item')
    def remove_item(self, request, pk=None):
        item_id = request.data.get('item_id')
        try:
            pool = PoolOrderService.remove_item(request.user, pk, item_id)
            return Response(OrderPoolSerializer(pool, context={'request': request}).data)
        except Exception as e:
            return Response({'error': str(e)}, status=400)


    @action(detail=True, methods=['POST'], url_path='lock')
    def lock(self, request, pk=None):
        pool = self.get_object()
        if pool.initiator != request.user:
            return Response({'error': 'Only the initiator can lock the pool'}, status=403)
        
        try:
            pool = PoolOrderService.lock_pool(pk)
            return Response(OrderPoolSerializer(pool, context={'request': request}).data)
        except Exception as e:
            return Response({'error': str(e)}, status=400)


    @action(detail=True, methods=['POST'], url_path='pay')
    def pay_share(self, request, pk=None):
        from domain.services.payment_services import InitiatePaymentService
        from apps.orders.models import PoolMember
        
        provider = request.data.get('provider')
        phone = request.data.get('phone')
        bank_code = request.data.get('bank_code')
        
        try:
            # 1. Identify the user's specific membership share
            member = PoolMember.objects.get(pool_id=pk, user=request.user)
            
            # 2. Trigger the universal payment flow
            result = InitiatePaymentService.execute(
                pool_member_id=member.id, 
                provider=provider, 
                phone=phone,
                bank_code=bank_code
            )
            
            payment = result['payment']
            gateway_data = result.get('gateway_response', {})
            
            return Response({
                "success": True,
                "payment_id": str(payment.id),
                "status": payment.status,
                **gateway_data
            })
        except PoolMember.DoesNotExist:
            return Response({"error": "You are not a member of this pool"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


    @action(detail=True, methods=['POST'], url_path='ready')
    def mark_ready(self, request, pk=None):
        try:
            pool = PoolOrderService.mark_member_ready(request.user, pk)
            return Response(OrderPoolSerializer(pool, context={'request': request}).data)
        except Exception as e:
            return Response({'error': str(e)}, status=400)


# Import models for the Q query
from django.db import models
