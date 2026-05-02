from celery import shared_task
import logging
from apps.orders.models import Order
from domain.services.order_services import CancelOrderService

logger = logging.getLogger(__name__)

@shared_task(name='domain.services.dispatch_tasks.dispatch_order_task')
def dispatch_order_task(order_id):
    """
    Asynchronous task to initiate driver dispatch for an order.
    """
    from domain.services.order_services import DispatchService
    try:
        DispatchService.execute(order_id)
    except Exception as e:
        logger.error(f"Dispatch failed for order {order_id}: {str(e)}")

@shared_task(name='domain.services.dispatch_tasks.retry_dispatch_task')
def retry_dispatch_task(order_id, attempt=1):
    """
    Multi-stage retry with exponential backoff and radius expansion.
    """
    from domain.services.order_services import DispatchService
    
    # Radius expansion based on attempt
    radii = {1: 15.0, 2: 25.0, 3: 50.0}
    radius = radii.get(attempt, 50.0)
    
    try:
        # Pass radius_km to expand the search area
        order = DispatchService.execute(order_id, is_retry=True, radius_km=radius)
        
        if not order.driver and attempt < 3:
            # Schedule next retry with backoff
            delays = {1: 120, 2: 300, 3: 900} 
            next_attempt = attempt + 1
            retry_dispatch_task.apply_async(
                args=[order_id, next_attempt],
                countdown=delays.get(attempt, 600)
            )
        elif not order.driver and attempt >= 3:
            logger.error(f"Dispatch permanently failed for Order {order_id} after 3 attempts.")
            
    except Exception as e:
        logger.error(f"Retry task error for order {order_id}: {str(e)}")

@shared_task(name='domain.services.dispatch_tasks.expire_unassigned_orders_task')
def expire_unassigned_orders_task():
    """
    Finds READY orders that haven't been assigned a driver within 30 minutes
    and cancels them automatically using row-level locking for safety.
    """
    from django.utils import timezone
    from datetime import timedelta
    from django.db import transaction
    from django.contrib.auth import get_user_model
    User = get_user_model()
    
    cutoff = timezone.now() - timedelta(minutes=30)
    
    # Ensure system dispatcher exists
    system_user, _ = User.objects.get_or_create(
        username='system_dispatcher',
        defaults={'role': 'admin', 'is_staff': True}
    )
    
    with transaction.atomic():
        stale_orders = Order.objects.select_for_update(skip_locked=True).filter(
            status='READY',
            driver__isnull=True,
            updated_at__lt=cutoff
        )
        
        count = stale_orders.count()
        if count > 0:
            logger.info(f"Auto-expiring {count} unassigned orders.")
            for order in stale_orders:
                try:
                    CancelOrderService.execute(
                        order.id, 
                        system_user, 
                        reason="Expired: No driver found within 30 minutes."
                    )
                except Exception as e:
                    logger.error(f"Failed to auto-expire order {order.id}: {str(e)}")
