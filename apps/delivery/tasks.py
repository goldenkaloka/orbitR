from celery import shared_task
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
from apps.delivery.models import Driver
from domain.fsm.driver_fsm import DriverFSM
import logging

logger = logging.getLogger(__name__)

STALE_THRESHOLD_MINUTES = 5

@shared_task
def check_stale_drivers():
    """
    Marks drivers as OFFLINE if they haven't sent a heartbeat in 5 minutes.
    This prevents phantom job dispatch to unreachable drivers.
    """
    threshold = timezone.now() - timedelta(minutes=STALE_THRESHOLD_MINUTES)

    stale_drivers = Driver.objects.filter(
        Q(status='AVAILABLE') | Q(status='DELIVERING'),
        Q(last_heartbeat__isnull=True) | Q(last_heartbeat__lt=threshold)
    )

    count = 0
    for driver in stale_drivers:
        try:
            driver.status = DriverFSM.transition(driver.status, 'OFFLINE')
            driver.save(update_fields=['status'])
            logger.info(f"Driver {driver.id} marked as OFFLINE (stale heartbeat)")
            count += 1
        except Exception as e:
            logger.error(f"Failed to mark driver {driver.id} as OFFLINE: {e}")

    return f"Marked {count} stale drivers as OFFLINE"


@shared_task
def clear_stale_heartbeats():
    """
    Clears stale last_heartbeat values for drivers that are OFFLINE.
    """
    Driver.objects.filter(status='OFFLINE').update(last_heartbeat=None)
    return "Cleared stale heartbeats for offline drivers"


@shared_task(acks_late=True, max_retries=3, default_retry_delay=5)
def broadcast_driver_location(order_id: str, lat: float, lng: float, heading: float):
    """
    Offloads WebSocket broadcast + push notification for driver location updates
    to Celery so the request-response cycle is not blocked.
    """
    from apps.events.utils import broadcast_order_location
    from infrastructure.notifications.service import NotificationService
    from django.core.cache import cache
    from apps.orders.models import Order
    from django.contrib.gis.geos import Point
    # 1. WebSocket broadcast
    broadcast_order_location(
        order_id=order_id,
        lat=lat,
        lng=lng,
        heading=heading,
    )

    # 2. Check proximity for "rider nearby" push notification
    try:
        order = Order.objects.get(id=order_id)
        if order.status == 'PICKED' and order.delivery_location:
            from apps.core.geo_utils import haversine_distance
            rider_loc = Point(lng, lat, srid=4326)
            dist_m = haversine_distance(order.delivery_location, rider_loc)
            if dist_m < 20:
                notif_cache_key = f"nearby_notif_{order.id}"
                if not cache.get(notif_cache_key):
                    NotificationService.send_push(
                        user_id=str(order.customer.id),
                        title="Rider Nearby! 🛵",
                        body=f"Your rider is almost at your location. PIN: {order.delivery_otp}. Please get ready!",
                        data={"order_id": str(order.id), "type": "rider_nearby"},
                    )
                    cache.set(notif_cache_key, True, 600)
    except Order.DoesNotExist:
        pass


@shared_task(acks_late=True, max_retries=2, default_retry_delay=10)
def notify_nearby_pool_created(pool_id: str, restaurant_name: str):
    """
    Finds users with active order/pool activity within 1 km of the new pool
    and sends them a push notification so they can discover and join.
    """
    from apps.orders.models import OrderPool, Order
    from infrastructure.notifications.service import NotificationService
    from django.contrib.gis.measure import D

    try:
        pool = OrderPool.objects.get(id=pool_id)
    except OrderPool.DoesNotExist:
        logger.warning(f"[NearbyPoolNotify] Pool {pool_id} not found")
        return f"Pool {pool_id} not found"

    # Active pools within 1 km
    nearby_pools = OrderPool.objects.filter(
        status__in=['OPEN', 'LOCKED', 'PAID', 'READY'],
        delivery_location__distance_lte=(pool.delivery_location, D(km=1)),
    ).exclude(id=pool.id).exclude(initiator=pool.initiator)

    user_ids: set[str] = set()
    for p in nearby_pools:
        user_ids.add(str(p.initiator_id))
        for m in p.members.select_related('user'):
            user_ids.add(str(m.user_id))

    # Active orders within 1 km
    nearby_orders = Order.objects.filter(
        status__in=['PENDING_PAYMENT', 'PAID', 'PREPARING', 'READY', 'DISPATCHING', 'DELIVERING'],
        delivery_location__distance_lte=(pool.delivery_location, D(km=1)),
    )
    for o in nearby_orders:
        user_ids.add(str(o.customer_id))

    # Exclude initiator and current members of the new pool
    for m in pool.members.all():
        user_ids.discard(str(m.user_id))
    user_ids.discard(str(pool.initiator_id))

    if not user_ids:
        logger.info(f"[NearbyPoolNotify] No nearby users to notify for pool {pool_id}")
        return f"No users to notify"

    sent = 0
    for uid in user_ids:
        try:
            NotificationService.send_push(
                user_id=uid,
                title="New Group Order Nearby!",
                body=f"{restaurant_name} has a group order near you. Tap to join!",
                data={
                    'type': 'new_nearby_pool',
                    'pool_id': pool_id,
                    'invite_code': pool.invite_code,
                },
            )
            sent += 1
        except Exception as e:
            logger.error(f"[NearbyPoolNotify] Failed to notify user {uid}: {e}")

    logger.info(f"[NearbyPoolNotify] Notified {sent}/{len(user_ids)} users about pool {pool_id}")
    return f"Notified {sent} users"