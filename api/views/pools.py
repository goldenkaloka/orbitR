from api.serializers.core import OrderPoolSerializer, PoolMemberSerializer
from api.serializers.utils import parse_coordinate_dict
from apps.orders.models import OrderPool
from django.db.models import Q
from domain.services.order_services import PoolOrderService
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle


class PoolOrderViewSet(viewsets.ModelViewSet):
    queryset = OrderPool.objects.all()
    serializer_class = OrderPoolSerializer
    permission_classes = [IsAuthenticated]

    #: Money-moving actions get a tighter budget than the blanket `user` rate
    #: so a compromised or buggy client cannot hammer the payment gateway.
    throttle_scope_map = {
        'pay_share': 'payments',
    }

    def get_throttles(self):
        scope = self.throttle_scope_map.get(self.action)
        if scope:
            self.throttle_scope = scope
            return [ScopedRateThrottle()]
        return super().get_throttles()

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

    @staticmethod
    def _matches_open_pool(pool, data):
        """Whether an existing open bucket is the one this request asked for."""
        requested_restaurant = data.get('restaurant')
        if not requested_restaurant:
            return False
        if str(pool.restaurant_id) != str(requested_restaurant):
            return False

        raw_scheduled = data.get('scheduled_for')
        if raw_scheduled in (None, ''):
            requested_scheduled = None
        else:
            from datetime import datetime

            from django.utils import timezone
            from django.utils.dateparse import parse_datetime

            requested_scheduled = parse_datetime(str(raw_scheduled))
            if requested_scheduled is None:
                try:
                    requested_scheduled = datetime.fromisoformat(
                        str(raw_scheduled).replace('Z', '+00:00'),
                    )
                except ValueError:
                    return False
            if timezone.is_naive(requested_scheduled):
                requested_scheduled = timezone.make_aware(
                    requested_scheduled, timezone.get_current_timezone(),
                )

        if pool.scheduled_for != requested_scheduled:
            return False

        # A different drop point/window is a different booking even if the
        # restaurant and time happen to match.
        requested_drop = data.get('drop_window')
        if str(pool.drop_window_id or '') != str(requested_drop or ''):
            return False

        # Older clients do not send a meal. When they omit it, the schedule
        # alone identifies the bucket; when they do send one, it must match.
        requested_meal = str(data.get('meal_period') or '').strip().upper() or None
        if requested_meal is None:
            return True
        return (pool.meal_period or None) == requested_meal

    def create(self, request, *args, **kwargs):
        # Returning an open bucket is only safe when it is the same booking.
        # Returning an unrelated restaurant/time would silently drop the
        # customer's new choice.
        existing_pool = OrderPool.objects.filter(initiator=request.user, status='OPEN').first()
        if existing_pool:
            if self._matches_open_pool(existing_pool, request.data):
                serializer = self.get_serializer(existing_pool, context={'request': request})
                return Response(serializer.data, status=status.HTTP_200_OK)
            return Response(
                {
                    'error': (
                        'You already have an open bucket. Finish or cancel it '
                        'before starting another one.'
                    ),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

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
            delivery_location=point,
            scheduled_for=serializer.validated_data.get('scheduled_for'),
            meal_period=serializer.validated_data.get('meal_period'),
            drop_window=serializer.validated_data.get('drop_window'),
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


    @action(detail=True, methods=['POST'], url_path='remove-member')
    def remove_member(self, request, pk=None):
        """Initiator or admin removes a member from the pool."""
        member_id = request.data.get('member_id')
        if not member_id:
            return Response({'error': 'member_id is required'}, status=400)
        try:
            pool = PoolOrderService.remove_member(pk, member_id, request.user)
            return Response(OrderPoolSerializer(pool, context={'request': request}).data)
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
        from apps.orders.models import PoolMember
        from domain.services.payment_services import InitiatePaymentService
        
        phone = request.data.get('phone')
        bank_code = request.data.get('bank_code')
        
        try:
            # 1. Identify the user's specific membership share
            member = PoolMember.objects.get(pool_id=pk, user=request.user)
            
            # 2. Trigger the universal payment flow
            result = InitiatePaymentService.execute(
                payable_type='pool_member',
                payable_id=member.id, 
                phone=phone,
                bank_code=bank_code,
                actor=request.user,
            )
            
            payment = result['payment']
            gateway_data = result.get('gateway_response', {})

            push_sent = gateway_data.get('success', False)
            base_response = {
                "success": push_sent,
                "payment_id": str(payment.id),
                "status": payment.status,
                "is_push": result.get('is_push', False),
            }

            if push_sent:
                base_response["provider_reference"] = gateway_data.get('provider_reference')
                base_response["message"] = gateway_data.get('message')
                base_response["instant_success"] = gateway_data.get('instant_success')
                if gateway_data.get('params'):
                    base_response["params"] = gateway_data['params']
                if gateway_data.get('endpoint'):
                    base_response["endpoint"] = gateway_data['endpoint']
            else:
                base_response["error"] = 'Payment initiation failed'
                base_response["code"] = gateway_data.get('code')

            return Response(base_response)
        except PoolMember.DoesNotExist:
            return Response({"error": "You are not a member of this pool"}, status=404)
        except Exception:
            return Response({"error": "Payment initiation failed."}, status=400)


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

    @action(detail=True, methods=['POST'], url_path='pool-decline')
    def decline_pool(self, request, pk=None):
        """Restaurant declines a paid pre-order bucket; every member is refunded."""
        try:
            pool = PoolOrderService.decline_pool(
                pk,
                request.user,
                reason=request.data.get('reason', 'Pre-order declined by restaurant'),
            )
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
            from django.contrib.gis.db.models.functions import Distance
            from django.contrib.gis.geos import Point
            try:
                user_location = Point(float(lng), float(lat), srid=4326)
                # Filter for pools within 1.0 km proximity (1000 meters).
                # Cap at 20 like the fallback branch — a dense city would
                # otherwise return a multi-megabyte single response.
                queryset = queryset.annotate(
                    distance=Distance('delivery_location', user_location)
                ).filter(distance__lte=1000).order_by('distance')[:20]
            except (ValueError, TypeError):
                return Response({'error': 'Invalid lat or lng'}, status=400)
        else:
            # Fallback to simple listing if coordinates not supplied
            queryset = queryset.order_by('-created_at')[:20]
            
        serializer = OrderPoolSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)


# Import models for the Q query
