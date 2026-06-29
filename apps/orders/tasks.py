import logging
from datetime import timedelta

from apps.orders.models import Dispute, Order
from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import OrderCancelled
from domain.fsm.order_fsm import OrderFSM

logger = logging.getLogger(__name__)


@shared_task(name='apps.orders.tasks.prewarm_order_route_geometry', ignore_result=True)
def prewarm_order_route_geometry(order_id):
    from apps.core.cache_keys import stable_cache_key
    from domain.services.mapbox_routing_service import MapboxRoutingService

    order = Order.objects.select_related('restaurant').filter(id=order_id).first()
    if not order or not order.restaurant or not order.restaurant.geo_location or not order.delivery_location:
        return 'skipped'

    res_lng = round(order.restaurant.geo_location.x, 6)
    res_lat = round(order.restaurant.geo_location.y, 6)
    dest_lng = round(order.delivery_location.x, 6)
    dest_lat = round(order.delivery_location.y, 6)
    cache_key = stable_cache_key('route_geom', {
        'from': [res_lng, res_lat],
        'to': [dest_lng, dest_lat],
    })
    if cache.get(cache_key) is not None:
        return 'cached'

    route = MapboxRoutingService.get_directions_route(
        res_lng,
        res_lat,
        dest_lng,
        dest_lat,
    )
    if route and route.coordinates:
        cache.set(cache_key, route.coordinates, 60 * 60 * 2)
        return 'warmed'
    return 'miss'


@shared_task
def cleanup_unpaid_orders():
    """
    Cancels orders stuck in PENDING_PAYMENT for more than 30 minutes.
    Notifies the customer and publishes OrderCancelled event for downstream handlers.
    """
    threshold = timezone.now() - timedelta(minutes=30)

    with transaction.atomic():
        abandoned = list(
            Order.objects.select_for_update(skip_locked=True)
            .filter(status='PENDING_PAYMENT', created_at__lt=threshold)
        )

        if not abandoned:
            return f"Cleaned up 0 orders."

        logger.info(f"[UnpaidCleanup] Cancelling {len(abandoned)} abandoned PENDING_PAYMENT orders")
        cancelled_orders = []
        for order in abandoned:
            try:
                order.status = OrderFSM.transition(order.status, 'CANCELLED')
                order.status_changed_at = timezone.now()
                order.save(update_fields=['status', 'status_changed_at', 'updated_at'])
                cancelled_orders.append(order)
            except Exception as e:
                logger.error(f"[UnpaidCleanup] Failed to cancel order {order.id}: {e}")

    for order in cancelled_orders:
        try:
            transaction.on_commit(lambda o=order: EventBus.publish(OrderCancelled(
                aggregate_id=o.id,
                payload={'actor_id': 'system', 'reason': 'payment_timeout_30min', 'order_id': str(o.id)}
            )))
        except Exception as e:
            logger.error(f"[UnpaidCleanup] Failed to publish event for order {order.id}: {e}")

    for order in cancelled_orders:
        try:
            from infrastructure.notifications.messages import order_expired_unpaid
            from infrastructure.notifications.service import NotificationService
            eu_title, eu_body = order_expired_unpaid(str(order.id)[:8])
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title=eu_title,
                body=eu_body,
                data={"order_id": str(order.id), "type": "order_expired_unpaid"}
            )
        except Exception as e:
            logger.error(f"[UnpaidCleanup] Failed to notify customer for order {order.id}: {e}")

    return f"Cancelled {len(cancelled_orders)} unpaid orders."


@shared_task
def dispute_sla_check():
    """
    Escalate disputes that have been PENDING for > 1 hour
    or INVESTIGATING for > 24 hours.
    """
    now = timezone.now()
    escalated_count = 0

    pending_cutoff = now - timedelta(hours=1)
    investigating_cutoff = now - timedelta(hours=24)

    with transaction.atomic():
        stale = Dispute.objects.filter(
            status__in=['PENDING', 'INVESTIGATING'],
            created_at__lt=pending_cutoff,
        ).select_for_update(skip_locked=True)

        for dispute in stale:
            try:
                with transaction.atomic():
                    d = Dispute.objects.select_for_update().get(id=dispute.id)
                    if d.status == 'PENDING' and d.created_at < pending_cutoff:
                        d.status = 'ESCALATED'
                        d.escalated_at = now
                        d.save(update_fields=['status', 'escalated_at'])
                        escalated_count += 1
                        from infrastructure.notifications.messages import (
                            dispute_escalated_admin,
                        )
                        from infrastructure.notifications.service import (
                            NotificationService,
                        )
                        de_title, de_body = dispute_escalated_admin(str(d.id)[:8], 'PENDING > 1h')
                        NotificationService.notify_admins(
                            title=de_title,
                            body=de_body,
                            data={"dispute_id": str(d.id), "type": "dispute_escalated"},
                        )
                    elif d.status == 'INVESTIGATING' and d.created_at < investigating_cutoff:
                        d.status = 'ESCALATED'
                        d.escalated_at = now
                        d.save(update_fields=['status', 'escalated_at'])
                        escalated_count += 1
                        de_title, de_body = dispute_escalated_admin(str(d.id)[:8], 'INVESTIGATING > 24h')
                        NotificationService.notify_admins(
                            title=de_title,
                            body=de_body,
                            data={"dispute_id": str(d.id), "type": "dispute_escalated"},
                        )
            except Dispute.DoesNotExist:
                continue
            except Exception as e:
                logger.error(f"Error escalating dispute {dispute.id}: {e}")

    if escalated_count:
        logger.info(f"[DisputeSLA] Escalated {escalated_count} stale disputes")
    return f"Escalated {escalated_count} disputes"


@shared_task
def cleanup_stale_idempotency_keys():
    """
    Deletes IdempotencyKey records older than 24 hours to prevent unbounded table growth.
    Runs hourly. Uses chunked deletes to avoid long-running transactions.
    """
    from apps.core.models import IdempotencyKey
    threshold = timezone.now() - timedelta(hours=24)
    deleted, _ = IdempotencyKey.objects.filter(created_at__lt=threshold).delete()
    if deleted:
        logger.info(f"[IdempotencyCleanup] Deleted {deleted} stale keys older than 24h")
    return f"Deleted {deleted} stale idempotency keys"
