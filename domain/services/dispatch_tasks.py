from celery import shared_task
from django.utils import timezone
from datetime import timedelta
from apps.orders.models import Order
import logging

logger = logging.getLogger(__name__)

@shared_task
def expire_unassigned_orders_task():
    """
    Cancel READY orders older than 30 minutes that have no driver assigned.
    Ensures that stale orders don't clutter the dispatch queue and 
    restaurants/customers are notified of the failure to find a rider.
    """
    cutoff = timezone.now() - timedelta(minutes=30)
    
    stale_orders = Order.objects.filter(
        status='READY',
        driver__isnull=True,
        created_at__lt=cutoff
    )
    
    count = stale_orders.count()
    if count > 0:
        logger.info(f"Found {count} stale unassigned orders. Cancelling...")
        # We update them in a loop to ensure any post_save signals or FSM logic triggers
        for order in stale_orders:
            try:
                # Transition status to CANCELLED
                order.status = 'CANCELLED'
                order.notes = (order.notes or "") + "\nAuto-cancelled: No rider found within 30 minutes."
                order.save()
                
                # TODO: Trigger notification events for Customer and Restaurant
                logger.info(f"Order {order.id} auto-cancelled due to timeout.")
            except Exception as e:
                logger.error(f"Failed to auto-cancel order {order.id}: {str(e)}")
    
    return f"Processed {count} stale orders."

@shared_task
def daily_earnings_reset_task():
    """
    Reset today_earnings for all drivers to 0 at midnight.
    """
    from apps.delivery.models import Driver
    updated = Driver.objects.update(today_earnings=0.0)
    logger.info(f"Daily reset: Reset earnings for {updated} drivers.")
    return f"Reset {updated} drivers."
