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
