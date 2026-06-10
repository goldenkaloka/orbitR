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
from infrastructure.notifications.messages import vendor_need_driver

logger = logging.getLogger(__name__)


def notify_restaurant_pending_batch(batch_id, restaurant_id, batch=None):
    """Notify a restaurant owner that their batch needs a driver assigned."""
    from apps.orders.models import DeliveryBatch
    from apps.restaurants.models import Restaurant
    from infrastructure.notifications.service import NotificationService

    try:
        if batch is None:
            batch = DeliveryBatch.objects.prefetch_related('orders').get(id=batch_id)

        restaurant = Restaurant.objects.select_related('owner').get(id=restaurant_id)
        if not restaurant.owner:
            return

        order_count = batch.orders.filter(restaurant_id=restaurant_id).count()
        if order_count == 0:
            return

        vn_title, vn_body = vendor_need_driver(order_count)
        NotificationService.send_push(
            user_id=str(restaurant.owner.id),
            title=vn_title,
            body=vn_body,
            data={
                "batch_id": str(batch_id),
                "restaurant_id": str(restaurant_id),
                "type": "batch_needs_driver",
            },
        )
    except Exception as exc:
        logger.error(f"[Dispatch] Failed to notify restaurant {restaurant_id}: {exc}")


class BatchDispatchService:
    """
    Notifies restaurants that their batch needs a driver.
    Restaurant assigns a temp driver via the assign-driver endpoint.
    """

    @staticmethod
    def dispatch_batch(batch_id: str):
        """
        Called after batch creation (via on_commit) or on retry.
        Transitions batch to PENDING_DRIVER and notifies all restaurants.
        """
        from apps.orders.models import DeliveryBatch

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

            # Notify all restaurants in the batch
            restaurant_ids = set(
                batch.orders.values_list('restaurant_id', flat=True)
            )
            for rid in restaurant_ids:
                notify_restaurant_pending_batch(batch_id, rid, batch)

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
