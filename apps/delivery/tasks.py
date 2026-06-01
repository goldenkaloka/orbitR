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