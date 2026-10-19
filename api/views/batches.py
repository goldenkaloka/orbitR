import logging

from api.serializers.core import DriverSerializer, OrderSerializer
from apps.delivery.models import Driver
from apps.orders.models import DeliveryBatch, Order
from domain.services.orders.order_assignment import AssignBatchService
from domain.services.permissions import can_driver_delegate, can_serve_restaurant
from infrastructure.notifications.messages import (
    batch_delegated_to_driver,
    batch_delegated_to_vendor,
)
from infrastructure.notifications.service import NotificationService
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

logger = logging.getLogger(__name__)


class BatchViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        if getattr(user, 'serialized_role', None) == 'admin':
            return DeliveryBatch.objects.all()
        if user.serialized_role == 'driver':
            return DeliveryBatch.objects.filter(driver__user=user)
        elif user.serialized_role in ('vendor', 'vendor_staff'):
            from apps.restaurants.models import Restaurant, SectionStaff
            owned_rids = Restaurant.objects.filter(owner=user).values_list('id', flat=True)
            staff_rids = SectionStaff.objects.filter(user=self.request.user).values_list('section__restaurant_id', flat=True)
            all_rids = set(owned_rids) | set(staff_rids)

            # A pooled batch can hold orders from several restaurants. Keep the
            # batch visible — the vendor still needs to prepare and track their
            # own orders — but never leak the other restaurants' orders. That
            # filtering happens in _visible_orders().
            return DeliveryBatch.objects.filter(
                orders__restaurant_id__in=all_rids
            ).distinct()
        return DeliveryBatch.objects.none()

    def _visible_orders(self, batches, user):
        """
        Orders from the requesting vendor's own restaurants only.

        A pooled batch belongs to several restaurants; a vendor must never
        receive anybody else's orders, so the batch stays visible while its
        foreign orders are filtered out here.

        The relation set mirrors OrderViewSet.get_queryset (core.py): without
        it every row triggers its own payment/driver/restaurant/items queries
        (DDIA Ch.1 — serializer N+1).
        """
        from apps.restaurants.models import Restaurant, SectionStaff
        if getattr(user, 'serialized_role', None) == 'admin':
            return (
                Order.objects
                .filter(batch__in=batches)
                .select_related(
                    'customer',
                    'restaurant',
                    'restaurant__owner',
                    'driver',
                    'driver__user',
                    'batch',
                    'payment',
                )
                .prefetch_related(
                    'items', 'items__food', 'items__section',
                    'pool__members', 'pool__members__user',
                )
                .distinct()
                .order_by('-created_at')
            )
        owned_rids = Restaurant.objects.filter(owner=user).values_list('id', flat=True)
        staff_rids = SectionStaff.objects.filter(user=user).values_list('section__restaurant_id', flat=True)
        all_rids = list(set(owned_rids) | set(staff_rids))
        return (
            Order.objects
            .filter(batch__in=batches, restaurant_id__in=all_rids)
            .select_related(
                'customer',
                'restaurant',
                'restaurant__owner',
                'driver',
                'driver__user',
                'batch',
                'payment',
            )
            .prefetch_related(
                'items', 'items__food', 'items__section',
                'pool__members', 'pool__members__user',
            )
            .distinct()
            .order_by('-created_at')
        )

    def list(self, request):
        # GenericViewSet.paginate_queryset caps limit at max_limit=100 — the
        # old code returned every order a vendor ever had in one response.
        queryset = self._visible_orders(self.get_queryset(), request.user)
        page = self.paginate_queryset(queryset)
        serializer = OrderSerializer(page if page is not None else queryset, many=True)
        if page is None:
            return Response(serializer.data)
        return self.get_paginated_response(serializer.data)
    
    def retrieve(self, request, pk=None):
        queryset = self.get_queryset()
        try:
            batch = queryset.get(id=pk)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found or access denied'}, status=404)
        orders = self._visible_orders([batch], request.user)
        return Response(OrderSerializer(orders, many=True).data)
    
    @action(detail=True, methods=['post'], url_path='assign-driver')
    def assign_driver(self, request, pk=None):
        """
        Allocate a driver to a batch.

        Authority follows Restaurant.dispatch_mode: in restaurant mode the
        vendor allocates, in admin mode an admin does. Admins may allocate in
        either mode — that is the escalation path when a restaurant goes
        quiet. The authoritative check also runs inside AssignBatchService;
        this gate only avoids the round trip.
        """
        if request.user.serialized_role not in ('admin', 'vendor', 'vendor_staff'):
            return Response({'error': 'Not authorised to allocate drivers'}, status=403)

        from domain.services.dispatch_routing import resolve_batch_dispatch_mode

        try:
            batch = DeliveryBatch.objects.get(id=pk)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=404)

        dispatch_mode = resolve_batch_dispatch_mode(batch)
        batch_is_pooled = batch.batch_type == 'MULTI_RESTAURANT'

        if request.user.serialized_role != 'admin' and dispatch_mode != 'restaurant':
            return Response({
                'error': 'Driver allocation for this restaurant is handled by admin dispatch.'
            }, status=403)

        # Pooled batches carry orders from several restaurants. Only OrbitR may
        # allocate them — a vendor would be allocating a run that includes a
        # competitor's orders.
        if request.user.serialized_role != 'admin' and batch_is_pooled:
            return Response({
                'error': 'Multi-restaurant batches are allocated by OrbitR dispatch.'
            }, status=403)

        driver_id = request.data.get('driver_id')
        if not driver_id:
            return Response({'error': 'driver_id is required'}, status=400)

        try:
            batch = AssignBatchService.execute(str(pk), str(driver_id), request.user)
            orders = batch.orders.select_related(
                'customer', 'restaurant', 'restaurant__owner', 'driver__user', 'payment',
            ).prefetch_related('items', 'items__food', 'items__section')
            return Response(OrderSerializer(orders, many=True).data)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=404)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)
        except Exception as e:
            return Response({'error': f'Failed to assign driver: {str(e)}'}, status=400)

    @action(detail=True, methods=['get'], url_path='delegate-candidates')
    def delegate_candidates(self, request, pk=None):
        """List drivers eligible for delegation (visible to the currently assigned driver)."""
        try:
            batch = DeliveryBatch.objects.get(id=pk)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=404)

        if not can_driver_delegate(request.user, batch):
            return Response({'error': 'Only the assigned driver with delegation permissions can delegate'}, status=403)

        first_order = batch.orders.select_related('restaurant', 'restaurant__owner').first()
        if not first_order:
            return Response({'error': 'Batch has no orders'}, status=400)

        from domain.services.permissions import restaurant_visible_drivers
        drivers = restaurant_visible_drivers(first_order.restaurant)
        available = [
            d for d in drivers
            if d.id != batch.driver.id
            and d.current_active_orders < d.max_active_orders
        ]
        return Response(DriverSerializer(available, many=True).data)

    @action(detail=True, methods=['post'], url_path='delegate')
    def delegate(self, request, pk=None):
        """
        Currently assigned driver delegates this batch to another driver.
        Body: { new_driver_id }
        The restaurant owner is notified of the change.
        """
        new_driver_id = request.data.get('new_driver_id')
        if not new_driver_id:
            return Response({'error': 'new_driver_id is required'}, status=400)

        try:
            batch = DeliveryBatch.objects.get(id=pk)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=404)

        # Verify the acting driver is allowed to delegate
        if not can_driver_delegate(request.user, batch):
            return Response({
                'error': 'Only the currently assigned driver with delegation permission (can_assign=True) can delegate this batch.'
            }, status=403)

        old_driver = batch.driver
        old_driver_name = old_driver.user.username if old_driver else 'Unknown'

        try:
            batch = AssignBatchService.execute(str(pk), str(new_driver_id), request.user)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)
        except PermissionError as e:
            return Response({'error': str(e)}, status=403)

        # ── Notify the restaurant owner about the delegation ──
        new_driver = batch.driver
        new_driver_name = new_driver.user.username if new_driver else 'Unknown'
        first_order = batch.orders.select_related('restaurant', 'restaurant__owner').first()
        restaurant_owner_id = str(first_order.restaurant.owner.id) if first_order else None

        if restaurant_owner_id:
            batch_short = str(batch.id)[:8].upper()
            v_title, v_body = batch_delegated_to_vendor(batch_short, old_driver_name, new_driver_name)
            NotificationService.send_push(
                user_id=restaurant_owner_id,
                title=v_title,
                body=v_body,
                data={
                    "batch_id": str(batch.id),
                    "type": "batch_delegated",
                    "old_driver_name": old_driver_name,
                    "new_driver_name": new_driver_name,
                },
            )

            # Also notify via WebSocket
            from apps.events.utils import broadcast_order_status
            for order in batch.orders.all():
                broadcast_order_status(str(order.id), 'ASSIGNED')

        # ── Notify the new driver ──
        if new_driver and first_order:
            d_title, d_body = batch_delegated_to_driver(batch_short, first_order.restaurant.name)
            NotificationService.send_push(
                user_id=str(new_driver.user.id),
                title=d_title,
                body=d_body,
                data={
                    "batch_id": str(batch.id),
                    "type": "batch_delegated_to_you",
                },
            )

        logger.info(f"Batch {batch.id} delegated from {old_driver_name} to {new_driver_name}")

        orders = batch.orders.select_related(
            'customer', 'restaurant', 'restaurant__owner', 'driver__user', 'payment',
        ).prefetch_related('items', 'items__food', 'items__section')
        return Response({
            'message': f'Batch delegated to {new_driver_name}',
            'orders': OrderSerializer(orders, many=True).data,
        })

    @action(detail=True, methods=['get'], url_path='metadata')
    def batch_metadata(self, request, pk=None):
        """Get batch-level summary with progress"""
        try:
            batch = DeliveryBatch.objects.get(id=pk)
            orders = batch.orders.select_related('restaurant', 'customer').all()
            
            delivered = orders.filter(status='DELIVERED').count()
            total = orders.count()
            
            user = getattr(request, 'user', None)
            role = user.serialized_role if user and hasattr(user, 'serialized_role') else (user.role if user else None)

            result = {
                'id': str(batch.id),
                'status': batch.status,
                'batch_type': batch.batch_type,
                'total_orders': total,
                'delivered_count': delivered,
                'remaining_count': total - delivered,
                'route_distance_km': float(batch.route_distance_km) if batch.route_distance_km else None,
                'route_optimization': batch.route_optimization,
            }

            if role == 'admin':
                result['driver_payout'] = float(batch.driver_payout) if batch.driver_payout else None
                result['user_paid_total'] = float(batch.user_paid_total) if batch.user_paid_total else None
                result['company_profit'] = float(batch.company_profit) if batch.company_profit else None
            elif role in ('driver', 'vendor', 'vendor_staff'):
                result['driver_payout'] = float(batch.driver_payout) if batch.driver_payout else None

            if role in ('vendor', 'vendor_staff', 'customer', 'admin'):
                result['user_paid_total'] = float(batch.user_paid_total) if batch.user_paid_total else None

            result.update({
                'restaurants': [
                    {
                        'id': str(o.restaurant.id),
                        'name': o.restaurant.name,
                        'latitude': o.restaurant.geo_location.y if o.restaurant and o.restaurant.geo_location else None,
                        'longitude': o.restaurant.geo_location.x if o.restaurant and o.restaurant.geo_location else None,
                    }
                    for o in orders.select_related('restaurant').order_by('route_sequence')
                    if o.restaurant
                ],
                'orders_summary': [
                    {
                        'id': str(o.id),
                        'route_sequence': o.route_sequence,
                        'status': o.status,
                        'customer_name': o.customer.username,
                        'customer_phone': o.customer.phone,
                        'delivery_address': o.delivery_address,
                        'delivery_latitude': o.delivery_location.y if o.delivery_location else None,
                        'delivery_longitude': o.delivery_location.x if o.delivery_location else None,
                        'restaurant_name': o.restaurant.name if o.restaurant else 'Marketplace',
                        'restaurant_latitude': o.restaurant.geo_location.y if o.restaurant and o.restaurant.geo_location else None,
                        'restaurant_longitude': o.restaurant.geo_location.x if o.restaurant and o.restaurant.geo_location else None,
                    }
                    for o in orders.order_by('route_sequence')
                ],
            })

            return Response(result)
        except DeliveryBatch.DoesNotExist:
            return Response({'error': 'Batch not found'}, status=404)
