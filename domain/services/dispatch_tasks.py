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
    """Asynchronous task to initiate driver dispatch for an order."""
    try:
        DispatchService.execute(order_id)
    except Exception as e:
        logger.error(f"Dispatch failed for order {order_id}: {str(e)}")


# Fix #4 — Retry storm: use Celery's own self.retry() instead of manual
# apply_async scheduling. Celery tracks retries reliably via self.request.retries.
BATCH_DISPATCH_RETRY_DELAYS = {0: 30, 1: 60, 2: 120, 3: 300, 4: 600}


@shared_task(
    name='domain.services.dispatch_tasks.dispatch_batch_task',
    bind=True,
    max_retries=5,
    acks_late=True,
)
def dispatch_batch_task(self, batch_id: str, attempt: int = 0):
    """
    Dispatch a batch to a driver. Self-retrying with exponential backoff.

    Fix #4: Uses Celery's built-in self.retry() instead of manual apply_async
    scheduling, which previously caused retry storms when exceptions occurred
    without incrementing the attempt counter.
    """
    from domain.services.batch_dispatch_service import BatchDispatchService

    try:
        result = BatchDispatchService.dispatch_batch(str(batch_id))
        if result:
            logger.info(f"[dispatch_batch_task] Batch {batch_id} dispatched successfully")
            return

        # No driver found — schedule next attempt via Celery's retry mechanism
        delay = BATCH_DISPATCH_RETRY_DELAYS.get(self.request.retries, 60)
        logger.info(
            f"[dispatch_batch_task] No driver for batch {batch_id}. "
            f"Retry {self.request.retries + 1}/5 in {delay}s"
        )
        raise self.retry(countdown=delay)

    except self.MaxRetriesExceededError:
        logger.error(
            f"[dispatch_batch_task] Batch {batch_id} exhausted all retries — "
            "escalating to WAITING_TEMP_DRIVER"
        )
        from apps.orders.models import DeliveryBatch
        from domain.services.batch_fsm import BatchFSM
        from domain.services.batch_dispatch_service import handle_expired_batch

        try:
            with transaction.atomic():
                batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
                if batch.status in ('CREATED', 'OFFERED'):
                    batch.status = BatchFSM.transition(batch, 'MAX_ATTEMPTS')
                    batch.save(update_fields=['status'])
            handle_expired_batch(batch_id)
        except Exception as e:
            logger.error(f"Failed to escalate batch {batch_id} on retry exhaustion: {e}")

    except Exception as exc:
        # Only retry on transient errors (DB, network). Do NOT retry on
        # BatchFSMError or ValueError — those are permanent failures.
        from domain.services.batch_fsm import BatchFSMError
        if isinstance(exc, (BatchFSMError, ValueError)):
            logger.error(
                f"[dispatch_batch_task] Permanent failure for batch {batch_id}: {exc}"
            )
            return

        delay = BATCH_DISPATCH_RETRY_DELAYS.get(self.request.retries, 60)
        logger.warning(
            f"[dispatch_batch_task] Transient error for batch {batch_id}: {exc}. "
            f"Retry in {delay}s"
        )
        raise self.retry(exc=exc, countdown=delay)

@shared_task(name='domain.services.dispatch_tasks.expire_unassigned_orders_task')
def expire_unassigned_orders_task():
    """
    Finds READY orders that haven't been assigned a driver within 30 minutes
    and cancels them.

    Fix #15 — Cancel-outside-lock: cancellations now happen INSIDE the same
    select_for_update transaction, preventing a race where another worker
    could assign the order between lock release and cancellation.
    """
    cutoff = timezone.now() - timedelta(minutes=30)

    system_user, _ = User.objects.get_or_create(
        username='system_dispatcher',
        defaults={'role': 'admin', 'is_staff': True},
    )

    # Fix #15: fetch AND cancel inside the same transaction while locks are held
    with transaction.atomic():
        stale_orders = list(
            Order.objects.select_for_update(skip_locked=True)
            .filter(
                status='READY',
                driver__isnull=True,
            )
            .filter(
                Q(ready_at__lt=cutoff) | Q(ready_at__isnull=True, created_at__lt=cutoff)
            )
        )

        if not stale_orders:
            return

        logger.info(f"[ExpiryTask] Auto-expiring {len(stale_orders)} unassigned orders.")

        # Bulk cancel inside the same transaction while row locks are held
        order_ids_to_cancel = [o.id for o in stale_orders]
        Order.objects.filter(id__in=order_ids_to_cancel).update(
            status='CANCELLED'
        )

    # Emit events after transaction commits (no DB locks held)
    for order in stale_orders:
        try:
            from domain.events.bus import EventBus
            from domain.events.types import OrderCancelled
            EventBus.publish(OrderCancelled(
                aggregate_id=order.id,
                payload={
                    'reason': 'Expired: No driver found within 30 minutes.',
                    'actor_id': str(system_user.id),
                },
            ))
        except Exception as exc:
            logger.error(f"[ExpiryTask] Failed to emit cancel event for order {order.id}: {exc}")


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
    Driver.objects.update(today_earnings=0)
    logger.info("Daily earnings reset complete.")


# Kept for backwards compatibility — new code uses dispatch_batch_task above.
# This shim delegates to the new unified task.
@shared_task(name='domain.services.dispatch_tasks.retry_batch_dispatch_task')
def retry_batch_dispatch_task(batch_id, attempt=1):
    """Deprecated shim — delegates to dispatch_batch_task."""
    logger.warning(
        f"[retry_batch_dispatch_task] Deprecated task called for batch {batch_id}. "
        "Use dispatch_batch_task instead."
    )
    dispatch_batch_task.apply_async(args=[str(batch_id), attempt])


@shared_task(name='domain.services.dispatch_tasks.expire_stale_preparing_orders_task')
def expire_stale_preparing_orders_task():
    """
    Two-stage expiry for orders that the restaurant hasn't started preparing:

    Stage 1 (CONFIRMED → PREPARING):
        Orders in CONFIRMED for longer than CONFIRMED_TO_PREPARING_GRACE_MINUTES
        get auto-transitioned to PREPARING. Restaurant is notified via push + SMS.

    Stage 2 (PREPARING → CANCELLED):
        Orders in PREPARING for longer than PREPARING_ORDER_TIMEOUT_MINUTES
        get auto-cancelled and refunded. Customer is notified.

    This ensures orders don't get "stuck" at the restaurant indefinitely.
    """
    from django.conf import settings
    from domain.fsm.order_fsm import OrderFSM, InvalidStateTransitionError
    from infrastructure.notifications.service import NotificationService
    from domain.services.order_services import CancelOrderService

    confirmed_grace = getattr(settings, 'CONFIRMED_TO_PREPARING_GRACE_MINUTES', 20)
    preparing_timeout = getattr(settings, 'PREPARING_ORDER_TIMEOUT_MINUTES', 45)

    system_user, _ = User.objects.get_or_create(
        username='system_dispatcher',
        defaults={'role': 'admin', 'is_staff': True},
    )

    now = timezone.now()
    confirmed_cutoff = now - timedelta(minutes=confirmed_grace)
    preparing_cutoff = now - timedelta(minutes=preparing_timeout)

    with transaction.atomic():
        stale_confirmed = list(
            Order.objects.select_for_update(skip_locked=True)
            .filter(status='CONFIRMED', status_changed_at__lt=confirmed_cutoff)
        )

        if stale_confirmed:
            logger.info(f"[PrepExpiry] Auto-transitioning {len(stale_confirmed)} CONFIRMED orders to PREPARING")
            for order in stale_confirmed:
                try:
                    new_state = OrderFSM.transition(order.status, 'PREPARING')
                    order.status = new_state
                    order.status_changed_at = timezone.now()
                    order.save(update_fields=['status', 'status_changed_at', 'updated_at'])
                except InvalidStateTransitionError as e:
                    logger.error(f"[PrepExpiry] FSM error transitioning order {order.id}: {e}")

    for order in stale_confirmed:
        try:
            NotificationService.send_push(
                user_id=str(order.restaurant.owner.id),
                title="Order Overdue — Starting Preparation",
                body=(
                    f"Order #{str(order.id)[:8]} has been in CONFIRMED for "
                    f"over {confirmed_grace} min. It has been auto-marked as PREPARING. "
                    f"Please start cooking immediately."
                ),
                data={"order_id": str(order.id), "type": "order_overdue"}
            )
            NotificationService.send_sms(
                order.restaurant.owner.phone,
                f"Habari {order.restaurant.owner.username or 'Vendor'}, oda #{str(order.id)[:8]} haijaanza kupikwa tangu {confirmed_grace} dakika. Umeagizwa kuanza sasa."
            )
        except Exception as e:
            logger.error(f"[PrepExpiry] Failed to notify restaurant for order {order.id}: {e}")

    with transaction.atomic():
        stale_preparing = list(
            Order.objects.select_for_update(skip_locked=True)
            .filter(status='PREPARING', status_changed_at__lt=preparing_cutoff)
        )

        if not stale_preparing:
            return

        logger.info(f"[PrepExpiry] Auto-cancelling {len(stale_preparing)} stale PREPARING orders")

        cancelled_ids = []
        for order in stale_preparing:
            try:
                CancelOrderService.execute(
                    order_id=str(order.id),
                    actor=system_user,
                    reason=f'Restaurant exceeded PREPARING timeout of {preparing_timeout} minutes without marking READY'
                )
                cancelled_ids.append(str(order.id))
            except Exception as e:
                logger.error(f"[PrepExpiry] Failed to cancel order {order.id}: {e}")

    for order_id in cancelled_ids:
        try:
            order = Order.objects.select_related('customer').get(id=order_id)
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title="Order Cancelled — Restaurant Delay",
                body=(
                    f"Your order #{str(order.id)[:8]} was cancelled because the restaurant "
                    f"did not start preparation within {preparing_timeout} minutes. "
                    f"A full refund is being processed."
                ),
                data={"order_id": str(order.id), "type": "order_cancelled_timeout"}
            )
            NotificationService.send_sms(
                order.customer.phone,
                f"Habari! Oda yako #{str(order.id)[:8]} imefutwa kwa sababu restranti hakupika ndani ya {preparing_timeout} dakika. Uttapokea ufufuzi wa malipo yako bila kupita muda mrefu."
            )
        except Exception as e:
            logger.error(f"[PrepExpiry] Failed to notify customer for order {order_id}: {e}")

    logger.info(f"[PrepExpiry] Completed. Cancelled {len(cancelled_ids)} orders.")


@shared_task(name='domain.services.dispatch_tasks.retry_dispatch_task')
def retry_dispatch_task(order_id: str, attempt: int = 1):
    """
    Retry dispatch for an order after initial failure.
    Uses exponential backoff based on attempt number.
    """

    max_attempts = 3
    retry_delays = {1: 120, 2: 300, 3: 900}

    if attempt > max_attempts:
        logger.warning(f"[retry_dispatch_task] Order {order_id} exhausted all retry attempts")
        from domain.events.bus import EventBus
        from domain.events.types import DispatchFailed
        EventBus.publish(DispatchFailed(
            aggregate_id=order_id,
            payload={'order_id': order_id, 'attempt_retry': False, 'reason': 'max_retries_exceeded'}
        ))
        return

    try:
        logger.info(f"[retry_dispatch_task] Retrying dispatch for order {order_id}, attempt {attempt}/{max_attempts}")
        DispatchService.execute(order_id)
        logger.info(f"[retry_dispatch_task] Dispatch succeeded for order {order_id}")
    except Exception as e:
        logger.error(f"[retry_dispatch_task] Dispatch failed for order {order_id}: {str(e)}")
        delay = retry_delays.get(attempt, 120)
        retry_dispatch_task.apply_async(args=[str(order_id), attempt + 1], countdown=delay)
