import logging
from datetime import timedelta

from apps.orders.models import Order
from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from domain.services.order_services import CancelOrderService
from infrastructure.notifications.messages import (
    order_cancelled_restaurant_delay,
    vendor_order_overdue,
)

logger = logging.getLogger(__name__)
User = get_user_model()

@shared_task(
    name='domain.services.dispatch_tasks.dispatch_batch_task',
)
def dispatch_batch_task(batch_id: str):
    """
    Request an admin allocation for a batch sitting in PENDING_DRIVER.

    Admin is the sole allocation authority, so this no longer notifies the
    restaurant — it publishes DriverDispatchRequested, and dispatch_handlers
    turns that into the admin SMS + push alert plus the realtime queue push.

    The first alert fires immediately, then reminders are throttled to
    BATCH_DRIVER_ALERT_REMINDER_MINUTES while the batch stays in
    PENDING_DRIVER. The lifecycle poll runs frequently, so this must stay
    idempotent across workers.
    """
    from apps.orders.models import DeliveryBatch
    from domain.events.bus import EventBus
    from domain.events.types import DriverDispatchRequested

    lock_key = f'dispatch_batch_lock:{batch_id}'
    lock_ttl = 60

    acquired = cache.add(lock_key, '1', lock_ttl)
    if not acquired:
        return

    try:
        reminder_minutes = getattr(settings, 'BATCH_DRIVER_ALERT_REMINDER_MINUTES', 10)
        reminder_cutoff = timezone.now() - timedelta(minutes=reminder_minutes)

        with transaction.atomic():
            batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
            if batch.status != 'PENDING_DRIVER':
                logger.info(
                    f"[dispatch_batch_task] Batch {batch_id} status '{batch.status}' — skipping"
                )
                return

            if batch.offer_sent_at and batch.offer_sent_at > reminder_cutoff:
                logger.info(
                    f"[dispatch_batch_task] Batch {batch_id} already alerted at "
                    f"{batch.offer_sent_at}; next reminder after {reminder_minutes} minutes"
                )
                return

            is_reminder = batch.offer_sent_at is not None
            batch.offer_sent_at = timezone.now()
            batch.save(update_fields=['offer_sent_at', 'updated_at'])

            orders = list(batch.orders.select_related('restaurant').all())
            order_count = len(orders)
            restaurant_ids = sorted({str(o.restaurant_id) for o in orders if o.restaurant_id})
            restaurant_names = sorted({o.restaurant.name for o in orders if o.restaurant})
            restaurant_name = (
                restaurant_names[0] if len(restaurant_names) == 1
                else f"{len(restaurant_names)} restoranti"
            )

            waiting_since = batch.waiting_since or batch.created_at
            waiting_minutes = max(
                0, int((timezone.now() - waiting_since).total_seconds() // 60)
            )

            from domain.services.dispatch_routing import resolve_batch_dispatch_mode
            dispatch_mode = resolve_batch_dispatch_mode(batch)

        logger.info(
            f"[dispatch_batch_task] Batch {batch_id} awaiting allocation "
            f"({dispatch_mode}): {order_count} orders, waiting {waiting_minutes} min"
        )

        EventBus.publish(DriverDispatchRequested(
            aggregate_id=batch.id,
            payload={
                'order_count': order_count,
                'restaurant_ids': restaurant_ids,
                'restaurant_name': restaurant_name,
                'waiting_minutes': waiting_minutes,
                'is_reminder': is_reminder,
                'dispatch_mode': dispatch_mode,
            },
        ))
    except DeliveryBatch.DoesNotExist:
        logger.warning(f"[dispatch_batch_task] Batch {batch_id} not found")
    except Exception as exc:
        logger.error(f"[dispatch_batch_task] Error requesting allocation for batch {batch_id}: {exc}", exc_info=True)
    finally:
        cache.delete(lock_key)

@shared_task(name='domain.services.dispatch_tasks.batch_lifecycle_poll_task')
def batch_lifecycle_poll_task():
    """
    Periodic task that advances batches through their lifecycle:
    1. Close ACCUMULATING batches that hit max orders or window expired
    2. FINALIZING for batches past SOFT_LOCK window
    3. Check section timeouts → PARTIAL_BATCH
    4. Check for all-sections-ready → PENDING_DRIVER
    """
    from domain.services.batch_pipeline import BatchPipeline
    BatchPipeline.close_due_accumulating_batches()
    BatchPipeline.check_and_expire_soft_lock()
    BatchPipeline.check_and_expire_pending_driver()
    BatchPipeline.check_section_timeouts()
    BatchPipeline.check_all_sections_ready()
    BatchPipeline.close_finished_batches()


@shared_task(name='domain.services.dispatch_tasks.pre_assign_drivers_for_batch')
def pre_assign_drivers_for_batch(batch_id: str):
    """
    Pre-warm: find available drivers for a batch that has entered PRE_ASSIGNING.
    Pings the top candidate driver so they start moving before food is fully ready.
    """
    from apps.orders.models import DeliveryBatch
    from domain.services.permissions import restaurant_visible_drivers
    from infrastructure.notifications.channels import default_channel
    from infrastructure.notifications.messages import vendor_need_driver

    try:
        batch = DeliveryBatch.objects.get(id=batch_id)
        if batch.status != 'PRE_ASSIGNING':
            return

        drivers = restaurant_visible_drivers(batch.restaurant)
        candidate = None
        for d in drivers:
            if d.driver_type == 'platform' and d.current_active_orders < d.max_active_orders:
                candidate = d
                break

        if candidate:
            default_channel.send_push(
                user_id=str(candidate.user.id),
                title="Upcoming Delivery",
                body=f"A batch at {batch.restaurant.name} will be ready soon. Stand by.",
                data={
                    "batch_id": batch_id,
                    "restaurant_id": str(batch.restaurant.id) if batch.restaurant else None,
                    "type": "batch_pre_assign",
                },
            )
            logger.info(f"[PreAssign] Pre-warmed driver {candidate.user.username} for batch {batch_id}")
        else:
            logger.info(f"[PreAssign] No available driver to pre-warm for batch {batch_id}")
    except Exception as exc:
        logger.error(f"[PreAssign] Failed for batch {batch_id}: {exc}")


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
        # Build the exclusion set using a subquery to avoid a LEFT OUTER JOIN
        # on the nullable batch FK. Postgres refuses SELECT FOR UPDATE on the
        # nullable side of an outer join, which is what .exclude(batch__status__in=[...])
        # generates. Fetching the IDs separately avoids that entirely.
        active_batch_order_ids = Order.objects.filter(
            batch__status__in=['ACCUMULATING', 'SOFT_LOCK', 'FINALIZING', 'PRE_ASSIGNING', 'PENDING_DRIVER', 'DISPATCHED']
        ).values_list('id', flat=True)

        stale_orders = list(
            Order.objects.select_for_update(skip_locked=True)
            .filter(
                status='READY',
                driver__isnull=True,
            )
            .filter(
                Q(ready_at__lt=cutoff) | Q(ready_at__isnull=True, created_at__lt=cutoff)
            )
            # Exclude via subquery — no LEFT JOIN, safe with SELECT FOR UPDATE
            .exclude(id__in=active_batch_order_ids)
        )

        if not stale_orders:
            return

        logger.info(f"[ExpiryTask] Auto-expiring {len(stale_orders)} unassigned orders.")

        # Cancel through the domain service so refunds, driver release,
        # reliability metadata and OrderCancelled events stay consistent.
        cancelled_ids = []
        for order in stale_orders:
            try:
                CancelOrderService.execute(
                    order_id=str(order.id),
                    actor=system_user,
                    reason='Expired: No driver found within 30 minutes.',
                    category='system_timeout',
                    fault='SYSTEM',
                    affects_restaurant_reputation=False,
                )
                cancelled_ids.append(str(order.id))
            except Exception as exc:
                logger.error(f"[ExpiryTask] Failed to cancel order {order.id}: {exc}")

    # Service emits events after transaction commits.
    for order_id in cancelled_ids:
        logger.info(f"[ExpiryTask] Cancelled stale unassigned order {order_id}")


LOCKED_POOL_TIMEOUT_MINUTES = 30


@shared_task(name='domain.services.dispatch_tasks.expire_stale_locked_pools_task')
def expire_stale_locked_pools_task():
    """
    Auto-expires LOCKED pools where members haven't paid within the timeout window.
    Uses lock_expires_at set by lock_pool for precision.
    Auto-refunds any members who already paid.
    """
    from apps.events.utils import broadcast_pool_status
    from apps.orders.models import OrderPool
    from domain.events.bus import EventBus
    from domain.events.types import PoolExpired
    from domain.fsm.pool_fsm import PoolFSM
    from domain.services.payment_services import RefundPaymentService

    stuck_pools = OrderPool.objects.filter(
        status='LOCKED',
        lock_expires_at__lt=timezone.now(),
    )

    count = 0
    for pool in stuck_pools:
        try:
            with transaction.atomic():
                pool_locked = OrderPool.objects.select_for_update().get(id=pool.id)
                if pool_locked.status != 'LOCKED':
                    continue
                pool_locked.status = PoolFSM.transition(pool_locked.status, 'EXPIRED')
                pool_locked.save(update_fields=['status'])

            paid_members = pool.members.filter(status='PAID')
            for member in paid_members:
                try:
                    RefundPaymentService.execute('pool_member', member.id)
                except Exception as e:
                    logger.error(f"Failed to refund pool member {member.id} during LOCKED pool expiry: {e}")

            _pool_id = str(pool.id)
            broadcast_pool_status(_pool_id, 'EXPIRED', message="Pool payment window expired. Refunding paid members.")
            EventBus.publish(PoolExpired(
                aggregate_id=pool.id,
                payload={'pool_id': _pool_id, 'reason': 'lock_timeout'},
            ))
            count += 1
        except Exception as e:
            logger.error(f"Failed to expire stuck LOCKED pool {pool.id}: {str(e)}")

    if count:
        logger.info(f"[Locked Pool Expiry] Expired {count} LOCKED pool(s) past {LOCKED_POOL_TIMEOUT_MINUTES}min timeout.")
    return count


@shared_task(name='domain.services.dispatch_tasks.expire_stale_pools_task')
def expire_stale_pools_task():
    """
    Auto-expires OPEN pools that have passed their expires_at deadline.
    Broadcasts EXPIRED status to all connected members via WebSocket.
    LOCKED pools are handled by expire_stale_locked_pools_task.
    """
    from apps.events.utils import broadcast_pool_status
    from apps.orders.models import OrderPool

    expired_pools = OrderPool.objects.filter(
        status='OPEN',
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
    from apps.orders.models import Order as MasterOrder
    from apps.orders.models import OrderPool
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
    from domain.fsm.order_fsm import InvalidStateTransitionError, OrderFSM
    from domain.services.order_services import CancelOrderService
    from infrastructure.notifications.service import NotificationService

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
            .select_related('restaurant__owner')
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
            from apps.restaurants.models import Restaurant
            if order.restaurant.source_type == Restaurant.SOURCE_PLATFORM_MANAGED:
                continue
            vo_title, vo_body = vendor_order_overdue(str(order.id)[:8], confirmed_grace)
            NotificationService.send_push(
                user_id=str(order.restaurant.owner.id),
                title=vo_title,
                body=vo_body,
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
                    reason=f'Restaurant exceeded PREPARING timeout of {preparing_timeout} minutes without marking READY',
                    category='restaurant_delay',
                    fault='VENDOR',
                    affects_restaurant_reputation=True,
                )
                cancelled_ids.append(str(order.id))
            except Exception as e:
                logger.error(f"[PrepExpiry] Failed to cancel order {order.id}: {e}")

    for order_id in cancelled_ids:
        try:
            order = Order.objects.select_related('customer').get(id=order_id)
            ocr_title, ocr_body = order_cancelled_restaurant_delay(str(order.id)[:8])
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title=ocr_title,
                body=ocr_body,
                data={"order_id": str(order.id), "type": "order_cancelled_timeout"}
            )
            NotificationService.send_sms(
                order.customer.phone,
                f"Habari! Oda yako #{str(order.id)[:8]} imefutwa kwa sababu restranti hakupika ndani ya {preparing_timeout} dakika. Uttapokea ufufuzi wa malipo yako bila kupita muda mrefu."
            )
        except Exception as e:
            logger.error(f"[PrepExpiry] Failed to notify customer for order {order_id}: {e}")

    logger.info(f"[PrepExpiry] Completed. Cancelled {len(cancelled_ids)} orders.")


@shared_task(name='domain.services.dispatch_tasks.expire_stale_assigned_orders_task')
def expire_stale_assigned_orders_task():
    """
    Cancels orders stuck in ASSIGNED state for more than 30 minutes.
    Handles cases where a driver accepted an order but never picked it up
    (e.g. GPS failures, app crashes, or driver inactivity).

    Uses CancelOrderService to properly release the driver, refund payment,
    delete the assignment, and publish the OrderCancelled event.
    """
    cutoff = timezone.now() - timedelta(minutes=30)

    system_user, _ = User.objects.get_or_create(
        username='system_dispatcher',
        defaults={'role': 'admin', 'is_staff': True},
    )

    stale_orders = Order.objects.filter(
        status='ASSIGNED',
        status_changed_at__lt=cutoff,
    )

    cancelled = 0
    for order in stale_orders:
        try:
            CancelOrderService.execute(
                order_id=str(order.id),
                actor=system_user,
                reason=(
                    'Expired: Driver did not pick up within 30 minutes '
                    'of assignment.'
                ),
                category='driver_no_show',
                fault='DRIVER',
                affects_restaurant_reputation=False,
            )
            cancelled += 1
        except Exception as exc:
            logger.error(
                f"[AssignedExpiry] Failed to cancel order {order.id}: {exc}"
            )

    if cancelled:
        logger.info(
            f"[AssignedExpiry] Cancelled {cancelled} stale ASSIGNED orders."
        )


@shared_task(
    name='domain.services.dispatch_tasks.expire_stale_picked_orders_task',
    acks_late=True,
)
def expire_stale_picked_orders_task():
    """
    Cancel orders stuck in PICKED state for more than 2 hours.
    This handles cases where a driver picked up an order but never marked it
    as delivered (e.g. app crash, lost device, or driver abandonment).

    Uses CancelOrderService to properly free the driver and handle refunds.
    """
    from domain.services.order_services import CancelOrderService

    cutoff = timezone.now() - timedelta(hours=2)

    system_user, _ = User.objects.get_or_create(
        username='system_dispatcher',
        defaults={'role': 'admin', 'is_staff': True},
    )

    stale_orders = Order.objects.filter(
        status='PICKED',
        status_changed_at__lt=cutoff,
    )

    cancelled = 0
    for order in stale_orders:
        try:
            CancelOrderService.execute(
                order_id=str(order.id),
                actor=system_user,
                reason=(
                    'Expired: Driver did not complete delivery within 2 hours '
                    'of pickup.'
                ),
                category='driver_no_show',
                fault='DRIVER',
                affects_restaurant_reputation=False,
            )
            cancelled += 1
        except Exception as exc:
            logger.error(
                f"[PickedExpiry] Failed to cancel order {order.id}: {exc}"
            )

    if cancelled:
        logger.info(
            f"[PickedExpiry] Cancelled {cancelled} stale PICKED orders."
        )


@shared_task(name='domain.services.dispatch_tasks.close_accumulating_batch_task')
def close_accumulating_batch_task(batch_id: str):
    """
    Transition a single accumulating batch to SOFT_LOCK.
    This is called via Celery delay/countdown when a batch is created.
    """
    from domain.services.batch_pipeline import BatchPipeline
    BatchPipeline._close_accumulating_batch(batch_id)


