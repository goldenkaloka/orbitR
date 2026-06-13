"""
BatchDispatchService — Notifies restaurants when a batch needs a driver.

Restaurants assign their own temp drivers (no platform auto-dispatch).
Simplified flow:
  CREATED → dispatch_batch() → PENDING_DRIVER → restaurant assigns driver → DISPATCHED
"""
import logging

from django.db import transaction
from django.utils import timezone

from domain.services.batch_fsm import BatchFSM, BatchFSMError
from domain.services.batch.notify_strategies import default_strategy
from domain.services.batch.interfaces import NotifyStrategy

logger = logging.getLogger(__name__)


class BatchDispatchService:
    """
    Notifies restaurants that their batch needs a driver.
    Restaurant assigns a temp driver via the assign-driver endpoint.

    Accepts an optional NotifyStrategy at construction time — swap it
    for different notification modes (push only, push+SMS, auto-assign, …).
    """

    def __init__(self, strategy: NotifyStrategy | None = None):
        self.strategy = strategy or default_strategy

    def dispatch_batch(self, batch_id: str):
        """
        Called after batch creation (via on_commit) or on retry.
        Transitions batch to PENDING_DRIVER and notifies all restaurants.
        """
        from apps.orders.models import DeliveryBatch
        from apps.restaurants.models import Restaurant

        try:
            batch = DeliveryBatch.objects.get(id=batch_id)
        except DeliveryBatch.DoesNotExist:
            logger.error(f"[Dispatch] Batch {batch_id} not found")
            return None

        if batch.status != 'CREATED':
            logger.warning(
                f"[Dispatch] Batch {batch_id} is in status '{batch.status}' — "
                f"expected CREATED"
            )
            return None

        try:
            with transaction.atomic():
                batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)

                if batch.status != 'CREATED':
                    return None

                batch.status = BatchFSM.transition(batch, 'WAIT_FOR_DRIVER')
                batch.waiting_since = timezone.now()
                batch.save(update_fields=['status', 'waiting_since'])

            logger.info(
                f"[Dispatch] Batch {batch_id} → PENDING_DRIVER. "
                f"Orders: {batch.orders.count()}"
            )

            restaurant_ids = set(
                batch.orders.values_list('restaurant_id', flat=True)
            )
            owners = {
                r.id: r.owner_id
                for r in Restaurant.objects.filter(id__in=restaurant_ids)
                .select_related('owner')
                .only('id', 'owner_id')
            }
            for rid in restaurant_ids:
                owner_id = owners.get(rid)
                if not owner_id:
                    continue
                order_count = batch.orders.filter(restaurant_id=rid).count()
                if order_count == 0:
                    continue
                self.strategy.notify_restaurant_pending(
                    batch_id=batch_id,
                    restaurant_id=str(rid),
                    restaurant_owner_id=str(owner_id),
                    order_count=order_count,
                )

            return batch

        except BatchFSMError as exc:
            logger.error(f"[Dispatch] FSM error for batch {batch_id}: {exc}")
            return None
        except Exception as exc:
            logger.error(
                f"[Dispatch] Error dispatching batch {batch_id}: {exc}",
                exc_info=True,
            )
            return None
