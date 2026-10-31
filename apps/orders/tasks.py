import logging
from datetime import timedelta

from apps.orders.models import Dispute, Order
from celery import shared_task
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import OrderCancelled
from domain.fsm.order_fsm import OrderFSM

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
                order.cancelled_at = timezone.now()
                order.cancelled_by = 'SYSTEM'
                order.cancellation_fault = 'NONE'
                order.cancellation_category = 'payment_timeout'
                order.cancellation_reason = 'Payment timeout before collection'
                order.affects_restaurant_reputation = False
                order.save(update_fields=[
                    'status', 'status_changed_at', 'cancelled_at', 'cancelled_by',
                    'cancellation_fault', 'cancellation_category',
                    'cancellation_reason', 'affects_restaurant_reputation',
                    'updated_at',
                ])
                cancelled_orders.append(order)
            except Exception as e:
                logger.error(f"[UnpaidCleanup] Failed to cancel order {order.id}: {e}")

    for order in cancelled_orders:
        try:
            transaction.on_commit(lambda o=order: EventBus.publish(OrderCancelled(
                aggregate_id=o.id,
                payload={
                    'actor_id': 'system',
                    'reason': 'payment_timeout_30min',
                    'order_id': str(o.id),
                    'cancelled_by': 'SYSTEM',
                    'cancellation_fault': 'NONE',
                    'cancellation_category': 'payment_timeout',
                    'affects_restaurant_reputation': False,
                }
            )))
        except Exception as e:
            logger.error(f"[UnpaidCleanup] Failed to publish event for order {order.id}: {e}")

    for order in cancelled_orders:
        try:
            from infrastructure.notifications.messages import order_expired_unpaid
            from infrastructure.notifications.service import NotificationService
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
                        from infrastructure.notifications.messages import (
                            dispute_escalated_admin,
                        )
                        from infrastructure.notifications.service import (
                            NotificationService,
                        )
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


@shared_task
def concierge_confirmation_timeout_check():
    """
    Auto-cancels concierge orders stuck in AWAITING_CONFIRMATION for > 10 minutes.
    Refunds the customer in full with platform fault and notifies them.
    """
    cutoff = timezone.now() - timedelta(minutes=10)
    with transaction.atomic():
        stale_orders = list(
            Order.objects.select_for_update(skip_locked=True)
            .filter(
                status='AWAITING_CONFIRMATION',
                status_changed_at__lt=cutoff,
            )
        )

    if not stale_orders:
        return "No stale concierge orders to clean up."

    from apps.users.models import User
    from domain.services.orders.order_cancellation import CancelOrderService
    from infrastructure.notifications.service import NotificationService

    system_user = User.objects.filter(role='admin').first()
    cancelled_count = 0

    for order in stale_orders:
        try:
            CancelOrderService.execute(
                order_id=str(order.id),
                actor=system_user or order.customer,
                reason="Restaurant confirmation timeout (10 minutes). Full refund issued.",
                category='system_timeout',
                fault='PLATFORM',
            )
            cancelled_count += 1
            try:
                NotificationService.send_push(
                    user_id=str(order.customer_id),
                    title="Order Cancelled — Restaurant Timeout",
                    body=(
                        f"We could not confirm order #{str(order.id)[:8]} with "
                        f"{order.restaurant.name} within 10 minutes. Your payment has been refunded in full."
                    ),
                    data={"order_id": str(order.id), "type": "concierge_timeout_refund"},
                )
            except Exception as ne:
                logger.warning(f"Could not notify customer for order {order.id}: {ne}")
        except Exception as e:
            logger.error(f"Failed to auto-cancel stale concierge order {order.id}: {e}")

    logger.info(f"[ConciergeTimeout] Cancelled {cancelled_count} stale orders.")
    return f"Auto-cancelled {cancelled_count} stale concierge orders."


@shared_task
def release_scheduled_orders():
    """
    Moves paid pre-orders whose prep window has started out of SCHEDULED and
    into the normal confirmed pipeline.
    """
    now = timezone.now()
    due_ids = list(
        Order.objects.filter(
            status='SCHEDULED',
            is_paid=True,
            release_at__lte=now,
        ).order_by('release_at').values_list('id', flat=True)[:200]
    )
    if not due_ids:
        return "Released 0 scheduled orders."

    # A bucket is one delivery: if any member order is due, release the rest
    # of that bucket in the same run even when the per-run cap truncated it.
    pool_ids = set(
        Order.objects.filter(id__in=due_ids, pool__isnull=False)
        .values_list('pool_id', flat=True)
    )
    if pool_ids:
        due_ids += list(
            Order.objects.filter(
                pool_id__in=pool_ids,
                status='SCHEDULED',
                is_paid=True,
            ).exclude(id__in=due_ids).values_list('id', flat=True)
        )

    from domain.services.orders.scheduled_orders import ReleaseScheduledOrderService

    released = 0
    released_pools = set()
    for order_id in due_ids:
        try:
            order = ReleaseScheduledOrderService.execute(order_id)
            if order.status != 'SCHEDULED':
                released += 1
                if order.pool_id:
                    released_pools.add(str(order.pool_id))
        except Exception as exc:
            logger.exception(f"[PreOrder] Failed to release order {order_id}: {exc}")

    # A bucket reaches the kitchen as one unit, so it gets one prep SMS once
    # all of its member orders are out of SCHEDULED.
    if released_pools:
        from domain.services.orders.order_pooling import _notify_pool_prep_sms

        for pool_id in released_pools:
            if Order.objects.filter(pool_id=pool_id, status='SCHEDULED').exists():
                continue
            try:
                _notify_pool_prep_sms(pool_id)
            except Exception as exc:
                logger.exception(
                    f"[PreOrder] Failed to notify kitchen for released pool {pool_id}: {exc}"
                )

    logger.info(f"[PreOrder] Released {released}/{len(due_ids)} scheduled orders.")
    return f"Released {released} scheduled orders."


@shared_task
def generate_preorder_sheets():
    """
    Freezes each meal's kitchen list once its bookings close.

    Runs often; the service skips meals that are still open and meals whose
    sheet already exists, so a re-run is cheap and never duplicates a sheet.
    """
    from domain.services.orders.preorder_sheets import generate_due_sheets

    generated = generate_due_sheets()
    return f"Generated {len(generated)} pre-order sheet(s)."


@shared_task
def close_due_drop_windows():
    """Apply the below-minimum policy when a drop window closes.

    A drop trip only pays for itself above ``min_orders``. At cut-off the count
    is finally known, so this is the moment to decide whether to run anyway,
    roll the orders to the next window, or cancel and refund.

    Runs every couple of minutes and claims each window occurrence with a cache
    key, so a re-run (or a second beat worker) never applies the policy twice.
    """
    from apps.orders.models import DropWindow
    from domain.services.orders.drop_service import (
        apply_low_fill_policy,
        cutoff_at,
        local_now,
    )

    now = local_now()
    handled = 0
    windows = (
        DropWindow.objects.filter(is_active=True, drop_point__is_active=True)
        .select_related('drop_point')
    )

    for window in windows:
        # The cut-off and the hand-over are on the same local day.
        for offset in (0, 1):
            day = now.date() - timedelta(days=offset)
            if window.weekday is not None and day.weekday() != window.weekday:
                continue
            cutoff = cutoff_at(window, day)
            # Only act in the short window after cut-off. A beat worker that
            # was down for hours must not reopen yesterday's decisions.
            if not (cutoff <= now < cutoff + timedelta(minutes=15)):
                continue

            key = f'drop-cutoff:{window.id}:{day.isoformat()}'
            if not cache.add(key, 1, timeout=60 * 60 * 26):
                continue

            try:
                outcome = apply_low_fill_policy(window, day, now=now)
                handled += 1
                logger.info(
                    "[DropCutoff] %s on %s → %s", window, day, outcome,
                )
            except Exception as exc:
                # Release the claim so the next beat retries; otherwise one bad
                # run would leave the window unresolved for the whole 26 hours.
                cache.delete(key)
                logger.exception(
                    "[DropCutoff] Failed to close %s on %s: %s", window, day, exc,
                )

    return f"Closed {handled} drop window(s)."
