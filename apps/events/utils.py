import json
import logging
from django.core.serializers.json import DjangoJSONEncoder
from .tasks import centrifugo_publish, centrifugo_broadcast

logger = logging.getLogger(__name__)

def broadcast_order_status(order_id, status, message="Status updated"):
    from apps.orders.models import Order

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

        channels = [f'orders:{order_id}', f'users:{order.customer_id}']

        if order.restaurant:
            channels.append(f'vendors:{order.restaurant.owner_id}')

        if order.driver:
            channels.append(f'riders:{order.driver.user_id}')

        centrifugo_broadcast.delay(channels, payload)

        if order.pool:
            for member in order.pool.members.all():
                if member.user_id != order.customer_id:
                    centrifugo_publish.delay(f'users:{member.user_id}', payload)

        if status == 'READY' and not order.driver:
            ping_payload = {
                'type': 'new_order_ping',
                'order_id': str(order_id),
                'restaurant_name': order.restaurant.name,
                'delivery_fee': float(order.delivery_fee)
            }
            centrifugo_publish.delay('riders_global:pool', ping_payload)

        if status == 'OFFERED' and order.driver:
            from infrastructure.notifications.service import NotificationService
            from infrastructure.notifications.messages import single_order_offered
            so_title, so_body = single_order_offered(order.restaurant.name)
            NotificationService.send_push(
                user_id=str(order.driver.user_id),
                title=so_title,
                body=so_body,
                data={
                    'type': 'new_order',
                    'order_id': str(order_id),
                    'restaurant_name': order.restaurant.name,
                },
            )

        if status in ('OFFERED', 'ASSIGNED', 'PICKED', 'DELIVERED', 'CANCELLED') and (order.driver or status == 'OFFERED'):
            centrifugo_publish.delay('riders_global:pool', {
                'type': 'order_removed_from_pool',
                'order_id': str(order_id),
                'status': status,
                'action': 'refresh_available_jobs'
            })

    except Order.DoesNotExist:
        pass
    except Exception as e:
        logger.warning(f"broadcast_order_status failed for order {order_id}: {e}")


def broadcast_pool_status(pool_id, status, message="Pool updated"):
    from apps.orders.models import OrderPool
    from api.serializers.core import OrderPoolSerializer

    try:
        pool = OrderPool.objects.prefetch_related('members', 'members__user').get(id=pool_id)
        serializer = OrderPoolSerializer(pool, exclude_fields=['restaurant_menu'])

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
            'member_map': member_map,
            'initiator_id': initiator_id,
        }

        clean_payload = json.loads(json.dumps(payload, cls=DjangoJSONEncoder))
        centrifugo_publish.delay(f'pools:{pool_id}', clean_payload)

    except OrderPool.DoesNotExist:
        pass
    except Exception as e:
        logger.warning(f"broadcast_pool_status failed for pool {pool_id}: {e}")


def broadcast_order_location(order_id, lat, lng, heading=0.0):
    payload = {
        'type': 'location_update',
        'order_id': str(order_id),
        'lat': float(lat),
        'lng': float(lng),
        'heading': float(heading)
    }
    centrifugo_publish.delay(f'orders:{order_id}', payload)


def broadcast_demand_update(demand_id, type_tag, extra=None):
    payload = {
        'type': type_tag,
        'demand_id': str(demand_id),
    }
    if extra:
        payload.update(extra)
    centrifugo_publish.delay(f'demands:{demand_id}', payload)


def broadcast_new_demand_to_sellers(demand_id, seller_ids, demand_title,
                                     buyer_h3, lat, lng, radius_km):
    channels = [f'users:{sid}' for sid in seller_ids]
    payload = {
        'type': 'new_demand',
        'demand_id': str(demand_id),
        'title': demand_title,
        'buyer_h3': buyer_h3,
        'lat': lat,
        'lng': lng,
        'radius_km': radius_km,
    }
    centrifugo_broadcast.delay(channels, payload)
