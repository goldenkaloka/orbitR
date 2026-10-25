import logging

from domain.events.bus import EventBus
from domain.events.types import (
    PoolCancelled,
    PoolCompleted,
    PoolLocked,
    PoolPaid,
    PoolReady,
)
from infrastructure.notifications.messages import (
    admin_pool_cancelled_batch_transit,
    pool_cancelled,
    pool_completed,
    pool_locked,
    pool_paid,
    preorder_booked_group,
    vendor_preorder_group_booked,
)
from infrastructure.notifications.service import NotificationService

logger = logging.getLogger(__name__)


def on_pool_locked(event):
    pool_id = event.payload.get('pool_id')
    try:
        from apps.orders.models import OrderPool
        pool = OrderPool.objects.prefetch_related('members__user').get(id=pool_id)
        pl_title, pl_body = pool_locked(pool.restaurant.name)
        for member in pool.members.all():
            if member.user_id:
                NotificationService.send_push(
                    user_id=str(member.user_id),
                    title=pl_title, body=pl_body,
                    data={'pool_id': str(pool_id), 'type': 'pool_locked'},
                )
    except Exception as e:
        logger.error(f"[PoolLocked] Failed to notify pool members for {pool_id}: {e}")


def on_pool_paid(event):
    pool_id = event.payload.get('pool_id')
    try:
        from apps.orders.models import OrderPool
        pool = (
            OrderPool.objects
            .select_related('restaurant')
            .prefetch_related('members__user')
            .get(id=pool_id)
        )

        # A scheduled bucket is not being cooked yet. Telling members the
        # restaurant is preparing now would be wrong, so confirm the booking
        # instead and notify the kitchen once about the upcoming slot.
        if pool.scheduled_for:
            from domain.services.orders.scheduled_orders import format_eating_time

            when_label = format_eating_time(pool.scheduled_for)
            pp_title, pp_body = preorder_booked_group(
                pool.restaurant.name, when_label,
            )
            if pool.restaurant.owner_id:
                v_title, v_body = vendor_preorder_group_booked(
                    pool.restaurant.name, when_label,
                )
                NotificationService.send_push(
                    user_id=str(pool.restaurant.owner_id),
                    title=v_title, body=v_body,
                    data={
                        'pool_id': str(pool_id),
                        'type': 'vendor_preorder_booked',
                    },
                )
        else:
            pp_title, pp_body = pool_paid(pool.restaurant.name)

        for member in pool.members.all():
            if member.user_id:
                NotificationService.send_push(
                    user_id=str(member.user_id),
                    title=pp_title, body=pp_body,
                    data={'pool_id': str(pool_id), 'type': 'pool_paid'},
                )
    except Exception as e:
        logger.error(f"[PoolPaid] Failed to notify pool members for {pool_id}: {e}")


def on_pool_cancelled(event):
    pool_id = event.payload.get('pool_id')
    batch_id = event.payload.get('batch_id')
    actor_id = event.payload.get('actor_id')

    try:
        from apps.orders.models import OrderPool
        pool = OrderPool.objects.prefetch_related('members__user').get(id=pool_id)
        pc_title, pc_body = pool_cancelled(pool.restaurant.name)
        # When the pool's orders are cancelled too, each member already gets an
        # OrderCancelled push naming their order — a pool-level push on top of
        # that is duplicate noise.
        members_notified_by_orders = bool(event.payload.get('orders_cancelled'))
        for member in pool.members.all():
            if (
                member.user_id
                and str(member.user_id) != actor_id
                and not members_notified_by_orders
            ):
                NotificationService.send_push(
                    user_id=str(member.user_id),
                    title=pc_title, body=pc_body,
                    data={'pool_id': str(pool_id), 'type': 'pool_cancelled'},
                )
    except Exception as e:
        logger.error(f"[PoolCancelled] Failed to notify pool members for {pool_id}: {e}")

    if not batch_id:
        return

    from apps.orders.models import DeliveryBatch
    try:
        batch = DeliveryBatch.objects.get(id=batch_id)
        active_orders = batch.orders.filter(status__in=['ASSIGNED', 'PICKED']).exists()
        if active_orders:
            logger.warning(
                f"[PoolCancelled] Pool {pool_id} cancelled but batch {batch_id} "
                f"has active orders — manual intervention required."
            )
            apt_title, apt_body = admin_pool_cancelled_batch_transit(
                str(pool_id)[:8], str(batch_id)[:8], batch.status
            )
            NotificationService.notify_admins(
                title=apt_title, body=apt_body,
                data={
                    'pool_id': str(pool_id),
                    'batch_id': str(batch_id),
                    'type': 'pool_cancel_late',
                },
            )
    except DeliveryBatch.DoesNotExist:
        pass


def on_pool_completed(event):
    pool_id = event.payload.get('pool_id')
    try:
        from apps.orders.models import OrderPool
        pool = OrderPool.objects.prefetch_related('members__user').get(id=pool_id)
        pco_title, pco_body = pool_completed(pool.restaurant.name)
        for member in pool.members.all():
            if member.user_id:
                NotificationService.send_push(
                    user_id=str(member.user_id),
                    title=pco_title, body=pco_body,
                    data={'pool_id': str(pool_id), 'type': 'pool_completed'},
                )
    except Exception as e:
        logger.error(f"[PoolCompleted] Failed to notify pool members for {pool_id}: {e}")


def on_pool_ready(event):
    from apps.orders.models import OrderPool
    from django.db import transaction
    from domain.services.batch_pipeline import BatchPipeline

    pool_id = event.aggregate_id
    try:
        pool = OrderPool.objects.select_related('restaurant').get(id=pool_id)
        if not pool.h3_index:
            logger.debug(f"[PoolReady] Pool {pool_id} has no h3_index — skipping batching")
            return

        result = BatchPipeline.on_pool_ready(pool)
        if result:
            with transaction.atomic():
                pool = OrderPool.objects.select_for_update().get(id=pool_id)
                if pool.status == 'READY':
                    pool.batch = result
                    pool.status = 'DISPATCHING'
                    pool.save(update_fields=['batch', 'status'])
            logger.info(f"[PoolReady] Pool {pool_id} dispatched via batch {result.id}")
        else:
            logger.debug(f"[PoolReady] No batch formed for pool {pool_id}, will retry in poll cycle")
    except Exception as e:
        logger.error(f"[PoolReady] Error processing pool {pool_id}: {e}", exc_info=True)


def register_handlers():
    EventBus.subscribe(PoolCancelled, on_pool_cancelled)
    EventBus.subscribe(PoolReady, on_pool_ready)
    EventBus.subscribe(PoolLocked, on_pool_locked)
    EventBus.subscribe(PoolPaid, on_pool_paid)
    EventBus.subscribe(PoolCompleted, on_pool_completed)
