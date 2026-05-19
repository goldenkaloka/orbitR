from celery import shared_task
from django.utils import timezone
from django.db import transaction
from datetime import timedelta
from apps.orders.models import Order
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
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title="Order Expired — Payment Not Received",
                body=f"Your order #{str(order.id)[:8]} has expired because payment was not completed within 30 minutes.",
                data={"order_id": str(order.id), "type": "order_expired_unpaid"}
            )
        except Exception as e:
            logger.error(f"[UnpaidCleanup] Failed to notify customer for order {order.id}: {e}")

    return f"Cancelled {len(cancelled_orders)} unpaid orders."


@shared_task
def check_waiting_batches_timeout():
    """
    Check for batches that have been waiting for temp drivers longer than the timeout (15 minutes).
    Expires them and marks orders for manual assignment.
    """
    from django.conf import settings
    from apps.orders.models import DeliveryBatch, Order
    from domain.services.batch_dispatch_service import handle_expired_batch

    timeout_minutes = getattr(settings, 'WAITING_BATCH_TIMEOUT_MINUTES', 15)
    threshold = timezone.now() - timedelta(minutes=timeout_minutes)

    expired_batches = DeliveryBatch.objects.filter(
        status='WAITING_TEMP_DRIVER',
        waiting_since__lt=threshold
    )

    count = 0
    for batch in expired_batches:
        try:
            logger.info(f"Batch {batch.id} timed out after {timeout_minutes} minutes in waiting status")
            batch.status = 'EXPIRED'
            batch.save(update_fields=['status'])

            orders = list(batch.orders.all())
            for order in orders:
                order.status = 'PENDING_MANUAL_ASSIGNMENT'
                order.batch = None

            Order.objects.bulk_update(orders, ['status', 'batch'])
            count += 1
        except Exception as e:
            logger.error(f"Error expiring batch {batch.id}: {e}")

    return f"Expired {count} waiting batches"