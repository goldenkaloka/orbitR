from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q
from apps.orders.models import OrderPool
from api.serializers.core import OrderPoolSerializer, PoolMemberSerializer
from api.serializers.utils import parse_coordinate_dict
from domain.services.order_services import PoolOrderService

class PoolOrderViewSet(viewsets.ModelViewSet):
    queryset = OrderPool.objects.all()
    serializer_class = OrderPoolSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        # Users see only active pools they initiated or joined.
        # Note: PoolMember.items is now a JSONField — no related prefetch needed.
        return OrderPool.objects.filter(
            Q(initiator=user) | Q(members__user=user),
            status__in=['OPEN', 'LOCKED', 'PAID', 'READY', 'DISPATCHING', 'DELIVERING']
        ).distinct().prefetch_related(
            'members', 'members__user',
            'restaurant__sections__foods',
            'final_orders',
        )

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
        restaurant = serializer.validated_data.get('restaurant')
        restaurant_id = getattr(restaurant, 'id', restaurant)

        point = parse_coordinate_dict(serializer.initial_data, 'delivery_location')

        pool = PoolOrderService.create_pool(
            initiator=self.request.user,
            restaurant_id=restaurant_id,
            delivery_address=serializer.validated_data.get('delivery_address'),
            delivery_location=point
        )
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
        # The client sends food_id (the item identifier in the JSON items list)
        food_id = request.data.get('food_id') or request.data.get('item_id')
        quantity = int(request.data.get('quantity', 1))
        if not food_id:
            return Response({'error': 'food_id is required'}, status=400)
        try:
            pool = PoolOrderService.remove_item(request.user, pk, food_id, quantity)
            return Response(OrderPoolSerializer(pool, context={'request': request}).data)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='leave')
    def leave(self, request, pk=None):
        """A non-initiator member exits the pool before it is locked."""
        try:
            PoolOrderService.leave_pool(request.user, pk)
            return Response({'success': True, 'message': 'You have left the pool.'})
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='cancel')
    def cancel(self, request, pk=None):
        """Initiator or admin cancels the pool entirely."""
        try:
            PoolOrderService.cancel_pool(pk, request.user)
            return Response({'success': True, 'message': 'Pool has been cancelled.'})
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except Exception as e:
            return Response({'error': str(e)}, status=400)


    @action(detail=True, methods=['POST'], url_path='lock')
    def lock(self, request, pk=None):
        try:
            # We pass the user to the service so it can check authorization while holding the lock
            pool = PoolOrderService.lock_pool(pk, request.user)
            return Response(OrderPoolSerializer(pool, context={'request': request}).data)
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
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
                payable_type='pool_member',
                payable_id=member.id, 
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
                "is_push": result.get('is_push', False),
                **gateway_data
            })
        except PoolMember.DoesNotExist:
            return Response({"error": "You are not a member of this pool"}, status=404)
        except Exception as e:
            return Response({"error": str(e)}, status=400)


    @action(detail=True, methods=['POST'], url_path='ready')
    def mark_ready(self, request, pk=None):
        """Member marks themselves as ready to pay (ADDING → READY)."""
        try:
            pool = PoolOrderService.mark_member_ready(request.user, pk)
            return Response(OrderPoolSerializer(pool, context={'request': request}).data)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=True, methods=['POST'], url_path='pool-ready')
    def mark_pool_ready(self, request, pk=None):
        """Restaurant marks the pool's food as ready for dispatch (PAID → READY)."""
        try:
            pool = PoolOrderService.mark_pool_ready(pk, request.user)
            return Response(OrderPoolSerializer(pool, context={'request': request}).data)
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except Exception as e:
            return Response({'error': str(e)}, status=400)

    @action(detail=False, methods=['GET'], url_path='nearby')
    def nearby(self, request):
        """Get active open pools within a 1.0 km radius of the user."""
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        
        # Filter for open pools that haven't expired yet
        from django.utils import timezone
        queryset = OrderPool.objects.filter(status='OPEN', expires_at__gt=timezone.now()).prefetch_related(
            'members', 'members__user',
            'restaurant__sections__foods',
            'final_orders',
        )
        
        if lat and lng:
            from django.contrib.gis.geos import Point
            from django.contrib.gis.db.models.functions import Distance
            try:
                user_location = Point(float(lng), float(lat), srid=4326)
                # Filter for pools within 1.0 km proximity (1000 meters)
                queryset = queryset.annotate(
                    distance=Distance('delivery_location', user_location)
                ).filter(distance__lte=1000).order_by('distance')
            except (ValueError, TypeError):
                return Response({'error': 'Invalid lat or lng'}, status=400)
        else:
            # Fallback to simple listing if coordinates not supplied
            queryset = queryset.order_by('-created_at')[:20]
            
        serializer = OrderPoolSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)


# Import models for the Q query
