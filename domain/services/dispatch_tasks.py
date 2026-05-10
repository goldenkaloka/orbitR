import logging
from datetime import timedelta
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from django.core.cache import cache
from django.contrib.auth import get_user_model

from apps.orders.models import Order
from domain.services.order_services import CancelOrderService, DispatchService

logger = logging.getLogger(__name__)
User = get_user_model()

MAX_DISPATCH_ATTEMPTS = 3
DISPATCH_RETRY_DELAYS = {1: 120, 2: 300, 3: 900}

@shared_task(name='domain.services.dispatch_tasks.dispatch_order_task')
def dispatch_order_task(order_id):
    """
    Asynchronous task to initiate driver dispatch for an order.
    """
    try:
        DispatchService.execute(order_id)
    except Exception as e:
        logger.error(f"Dispatch failed for order {order_id}: {str(e)}")

@shared_task(name='domain.services.dispatch_tasks.retry_dispatch_task')
def retry_dispatch_task(order_id, attempt=1):
    """
    Multi-stage retry with exponential backoff and radius expansion.
    """
    # Radius expansion based on attempt
    radii = {1: 15.0, 2: 25.0, 3: 50.0}
    radius = radii.get(attempt, 50.0)
    
    try:
        # Pass radius_km to expand the search area
        order = DispatchService.execute(order_id, is_retry=True, radius_km=radius)
        
        if not order.driver and attempt < MAX_DISPATCH_ATTEMPTS:
            # Schedule next retry with backoff
            next_attempt = attempt + 1
            lock_key = f"dispatch_retry_scheduled:{order_id}:{next_attempt}"
            # Prevent duplicate queueing from concurrent workers or duplicate triggers.
            if cache.add(lock_key, "1", timeout=DISPATCH_RETRY_DELAYS.get(attempt, 600) + 120):
                retry_dispatch_task.apply_async(
                    args=[order_id, next_attempt],
                    countdown=DISPATCH_RETRY_DELAYS.get(attempt, 600),
                )
        elif not order.driver and attempt >= MAX_DISPATCH_ATTEMPTS:
            logger.error(f"Dispatch permanently failed for Order {order_id} after {MAX_DISPATCH_ATTEMPTS} attempts.")
            
    except Exception as e:
        logger.error(f"Retry task error for order {order_id}: {str(e)}")

@shared_task(name='domain.services.dispatch_tasks.expire_unassigned_orders_task')
def expire_unassigned_orders_task():
    """
    Finds READY orders that haven't been assigned a driver within 30 minutes
    and cancels them automatically using row-level locking for safety.
    """
    cutoff = timezone.now() - timedelta(minutes=30)
    
    # Ensure system dispatcher exists
    system_user, _ = User.objects.get_or_create(
        username='system_dispatcher',
        defaults={'role': 'admin', 'is_staff': True}
    )
    
    with transaction.atomic():
        stale_order_ids = list(
            Order.objects.select_for_update(skip_locked=True).filter(
            status='READY',
            driver__isnull=True,
        ).filter(
            Q(ready_at__lt=cutoff) | Q(ready_at__isnull=True, created_at__lt=cutoff),
            ).values_list('id', flat=True)
        )

    if stale_order_ids:
        logger.info(f"Auto-expiring {len(stale_order_ids)} unassigned orders.")
    for order_id in stale_order_ids:
        try:
            CancelOrderService.execute(
                order_id,
                system_user,
                reason="Expired: No driver found within 30 minutes."
            )
        except Exception as e:
            logger.error(f"Failed to auto-expire order {order_id}: {str(e)}")
