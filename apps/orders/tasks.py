from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from apps.orders.models import Order
import logging

logger = logging.getLogger(__name__)

@shared_task
def cleanup_unpaid_orders():
    """
    Deletes orders that have been stuck in PENDING_PAYMENT for more than 30 minutes.
    This prevents 'Ghost Orders' from cluttering the database.
    """
    threshold = timezone.now() - timedelta(minutes=30)
    abandoned_orders = Order.objects.filter(
        status='PENDING_PAYMENT',
        is_paid=False,
        created_at__lt=threshold
    )

    count = abandoned_orders.count()
    if count > 0:
        logger.info(f"Cleaning up {count} abandoned PENDING_PAYMENT orders.")
        abandoned_orders.delete()

    return f"Cleaned up {count} orders."


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
