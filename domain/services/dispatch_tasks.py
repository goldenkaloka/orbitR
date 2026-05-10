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


@shared_task(name='domain.services.dispatch_tasks.expire_stale_pools_task')
def expire_stale_pools_task():
    """
    Auto-expires OPEN/LOCKED pools that have passed their expires_at deadline.
    Broadcasts EXPIRED status to all connected members via WebSocket.
    """
    from apps.orders.models import OrderPool
    from apps.events.utils import broadcast_pool_status

    expired_pools = OrderPool.objects.filter(
        status__in=['OPEN', 'LOCKED'],
        expires_at__lt=timezone.now()
    )

    count = 0
    for pool in expired_pools:
        try:
            with transaction.atomic():
                pool_locked = OrderPool.objects.select_for_update().get(id=pool.id)
                if pool_locked.status not in ('OPEN', 'LOCKED'):
                    continue  # Already transitioned by concurrent process
                pool_locked.status = 'EXPIRED'
                pool_locked.save(update_fields=['status'])
            # Notify outside of transaction so it always fires even if it's a noop
            broadcast_pool_status(str(pool.id), 'EXPIRED', message="This pool has expired.")
            count += 1
        except Exception as e:
            logger.error(f"Failed to expire pool {pool.id}: {str(e)}")

    if count:
        logger.info(f"[Pool Expiry] Expired {count} stale pool(s).")
    return count


@shared_task(name='domain.services.dispatch_tasks.recover_stuck_pools_task')
def recover_stuck_pools_task():
    """
    Safety net: Finds pools that are LOCKED with all members PAID
    but no master Order was ever created (e.g. due to a crash during finalize_pool).
    Re-triggers finalize_pool to recover these orphaned pools.
    """
    from apps.orders.models import OrderPool, Order as MasterOrder
    from domain.services.order_services import PoolOrderService

    # Find LOCKED pools where every member has PAID
    stuck_pools = OrderPool.objects.filter(status='LOCKED').prefetch_related('members')

    recovered = 0
    for pool in stuck_pools:
        members = pool.members.all()
        if not members.exists():
            continue

        all_paid = all(m.status == 'PAID' for m in members)
        if not all_paid:
            continue

        # Check if a master Order was already created for this pool
        if MasterOrder.objects.filter(pool=pool).exists():
            continue

        # This pool is stuck — all paid, no order created
        logger.warning(
            f"[Pool Recovery] Pool {pool.id} ({pool.invite_code}) is stuck: "
            f"LOCKED + all PAID but no master order. Re-running finalize_pool."
        )
        try:
            PoolOrderService.finalize_pool(pool.id)
            recovered += 1
            logger.info(f"[Pool Recovery] Successfully recovered pool {pool.id}")
        except Exception as e:
            logger.error(f"[Pool Recovery] Failed to recover pool {pool.id}: {str(e)}")

    if recovered:
        logger.info(f"[Pool Recovery] Recovered {recovered} stuck pool(s).")
    return recovered


@shared_task(name='domain.services.dispatch_tasks.daily_earnings_reset_task')
def daily_earnings_reset_task():
    """
    Resets driver daily_earnings at midnight.
    """
    from apps.delivery.models import Driver
    Driver.objects.update(daily_earnings=0)
    logger.info("Daily earnings reset complete.")
