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
        pool = OrderPool.objects.prefetch_related('members__user').get(id=pool_id)
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
        for member in pool.members.all():
            if member.user_id and str(member.user_id) != actor_id:
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

        pool_ref = _PoolBatchable(pool)
        result = BatchPipeline.run_for_pool(pool_ref)
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


class _PoolBatchable:
    def __init__(self, pool):
        self.id = pool.id
        self.h3_index = pool.h3_index
        self.restaurant = pool.restaurant
        self.restaurant_id = pool.restaurant_id
        self.delivery_location = pool.delivery_location
        self.delivery_address = pool.delivery_address
        self.customer = pool.initiator
        self.customer_id = pool.initiator_id
        self.status = 'READY'
        self.batch = None
        self.batch_id = None
        self.ready_at = pool.updated_at
        self.created_at = pool.created_at
        self.is_pool = True
        self.is_pool_order = True
        self.delivery_fee = pool.total_delivery_fee
        self.service_fee = pool.service_fee


def register_handlers():
    EventBus.subscribe(PoolCancelled, on_pool_cancelled)
    EventBus.subscribe(PoolReady, on_pool_ready)
    EventBus.subscribe(PoolLocked, on_pool_locked)
    EventBus.subscribe(PoolPaid, on_pool_paid)
    EventBus.subscribe(PoolCompleted, on_pool_completed)
