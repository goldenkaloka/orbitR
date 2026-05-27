from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

def broadcast_order_status(order_id, status, message="Status updated"):
    from apps.orders.models import Order
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    try:
        order = Order.objects.select_related('restaurant', 'driver').prefetch_related(
            'pool__members__user'
        ).get(id=order_id)
        payload = {
            'type': 'order_status_update',
            'order_id': str(order_id),
            'status': status,
            'message': message,
            'restaurant_id': str(order.restaurant_id),
            'driver_id': str(order.driver_id) if order.driver_id else None
        }

        # 1. Notify Customer (Order room & User room)
        async_to_sync(channel_layer.group_send)(f'order_{order_id}', payload)
        async_to_sync(channel_layer.group_send)(f'user_{order.customer_id}', payload)

        # 2. Notify Vendor (using owner user_id)
        if order.restaurant:
            async_to_sync(channel_layer.group_send)(f'vendor_{order.restaurant.owner_id}', payload)

        # 3. Notify Specific Rider (using user_id)
        if order.driver:
            async_to_sync(channel_layer.group_send)(f'rider_{order.driver.user_id}', payload)

        # 4. Priority 4 Fix: Notify ALL pool members (non-initiator members have no order visibility otherwise)
        if order.pool:
            for member in order.pool.members.all():
                if member.user_id != order.customer_id:  # Skip initiator (already notified via user_)
                    async_to_sync(channel_layer.group_send)(f'user_{member.user_id}', payload)

        # 5. Notify All Riders (New Ping) if status is READY and no driver
        if status == 'READY' and not order.driver:
            async_to_sync(channel_layer.group_send)('riders', {
                'type': 'new_order_ping',
                'order_id': str(order_id),
                'restaurant_name': order.restaurant.name,
                'delivery_fee': float(order.delivery_fee)
            })

        # 6. Send push notifications when a job is offered to a specific driver
        if status == 'OFFERED' and order.driver:
            from infrastructure.notifications.service import NotificationService
            NotificationService.send_push(
                user_id=str(order.driver.user_id),
                title="New Delivery Order!",
                body=f"Order from {order.restaurant.name} is ready for pickup.",
                data={
                    'type': 'new_order',
                    'order_id': str(order_id),
                    'restaurant_name': order.restaurant.name,
                }
            )

        # 7. Notify ALL Riders to refresh available jobs when order is claimed/accepted/assigned
        # This ensures the ping disappears for all other riders
        # For OFFERED, always remove from pool (order.driver may not be set in Phase 2)
        if status in ('OFFERED', 'ASSIGNED', 'PICKED', 'DELIVERED', 'CANCELLED') and (order.driver or status == 'OFFERED'):
            async_to_sync(channel_layer.group_send)('riders', {
                'type': 'order_removed_from_pool',
                'order_id': str(order_id),
                'status': status,
                'action': 'refresh_available_jobs'
            })
            
    except Order.DoesNotExist:
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"broadcast_order_status failed for order {order_id}: {e}")

def broadcast_pool_status(pool_id, status, message="Pool updated"):
    from apps.orders.models import OrderPool
    from api.serializers.core import OrderPoolSerializer
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    try:
        pool = OrderPool.objects.prefetch_related('members', 'members__user').get(id=pool_id)
        # Performance: Exclude the heavy menu from the real-time broadcast
        # The frontend already has the menu from the initial load
        serializer = OrderPoolSerializer(pool, exclude_fields=['restaurant_menu'])
        
        import json
        from django.core.serializers.json import DjangoJSONEncoder

        # Priority 3 Fix: Include a member_map so each Flutter client can self-identify
        # without needing request context. Format: {"user_id": "member_id"}
        member_map = {
            str(m.user_id): str(m.id)
            for m in pool.members.all()
        }
        initiator_id = str(pool.initiator_id)

        payload = {
            'type': 'pool_update',
            'pool_id': str(pool_id),
            'status': status,
            'message': message,
            'pool': serializer.data,
            'member_map': member_map,       # {user_id: member_id} for client-side identity
            'initiator_id': initiator_id,  # so each client knows if they are the initiator
        }

        # Sanitize payload for Channels msgpack/json serialization
        clean_payload = json.loads(json.dumps(payload, cls=DjangoJSONEncoder))

        # Notify the pool group
        async_to_sync(channel_layer.group_send)(f'pool_{pool_id}', clean_payload)
            
    except OrderPool.DoesNotExist:
        pass
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"broadcast_pool_status failed for pool {pool_id}: {e}")


def broadcast_order_location(order_id, lat, lng, heading=0.0):

    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    payload = {
        'type': 'location_update',
        'order_id': str(order_id),
        'lat': float(lat),
        'lng': float(lng),
        'heading': float(heading)
    }
    async_to_sync(channel_layer.group_send)(f'order_{order_id}', payload)
