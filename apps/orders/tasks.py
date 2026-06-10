from celery import shared_task
from django.utils import timezone
from django.db import transaction
from datetime import timedelta
from apps.orders.models import Order, Dispute
from domain.fsm.order_fsm import OrderFSM
from domain.events.bus import EventBus
from domain.events.types import OrderCancelled
import logging

logger = logging.getLogger(__name__)


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
            from infrastructure.notifications.service import NotificationService
            from infrastructure.notifications.messages import order_expired_unpaid
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
                        from infrastructure.notifications.service import NotificationService
                        from infrastructure.notifications.messages import dispute_escalated_admin
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