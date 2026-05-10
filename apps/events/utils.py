from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

def broadcast_order_status(order_id, status, message="Status updated"):
    from apps.orders.models import Order
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    try:
        order = Order.objects.get(id=order_id)
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

        # 4. Notify All Riders (New Ping) if status is READY and no driver
        if status == 'READY' and not order.driver:
            async_to_sync(channel_layer.group_send)('riders', {
                'type': 'new_order_ping',
                'order_id': str(order_id),
                'restaurant_name': order.restaurant.name,
                'delivery_fee': float(order.delivery_fee)
            })
            
    except Order.DoesNotExist:
        pass

def broadcast_pool_status(pool_id, status, message="Pool updated"):
    from apps.orders.models import OrderPool
    from api.serializers.core import OrderPoolSerializer
    channel_layer = get_channel_layer()
    if not channel_layer:
        return

    try:
        pool = OrderPool.objects.get(id=pool_id)
        # Performance: Exclude the heavy menu from the real-time broadcast
        # The frontend already has the menu from the initial load
        serializer = OrderPoolSerializer(pool, exclude_fields=['restaurant_menu'])
        
        payload = {
            'type': 'pool_update',
            'pool_id': str(pool_id),
            'status': status,
            'message': message,
            'pool': serializer.data,
        }

        # Notify the pool group
        async_to_sync(channel_layer.group_send)(f'pool_{pool_id}', payload)
            
    except OrderPool.DoesNotExist:
        pass


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
