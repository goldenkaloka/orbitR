from django.db import transaction
from django.core.cache import cache
import logging
from apps.core.geo_utils import get_neighbors

logger = logging.getLogger(__name__)

SINGLE_BATCH_LOCK_TTL = 15


class BatchingService:
    """
    Service responsible for grouping separate orders into Delivery Batches
    based on Restaurant and H3 proximity.
    """

    @staticmethod
    def attempt_batching(restaurant_id, h3_index):
        lock_key = f"single_batch_lock:{restaurant_id}:{h3_index[:8]}"
        lock_acquired = cache.add(lock_key, "1", SINGLE_BATCH_LOCK_TTL)

        if not lock_acquired:
            logger.debug(f"Another worker processing single batch for restaurant {restaurant_id}")
            return None

        try:
            return BatchingService._process_batching(restaurant_id, h3_index)
        finally:
            cache.delete(lock_key)

    @staticmethod
    @transaction.atomic
    def _process_batching(restaurant_id, h3_index):
        from apps.orders.models import Order, DeliveryBatch

        neighbor_cells = get_neighbors(h3_index, 1)

        orders = list(Order.objects.select_for_update(
            skip_locked=True
        ).filter(
            status='READY',
            restaurant_id=restaurant_id,
            h3_index__in=neighbor_cells,
            batch__isnull=True
        ))

        count = len(orders)

        if count > 1:
            logger.info(f"Creating batch for {count} orders at Restaurant {restaurant_id} -> H3 Cell {h3_index}")

            batch = DeliveryBatch.objects.create(
                restaurant_id=restaurant_id,
                batch_type='SINGLE_RESTAURANT',
                status='CREATED'
            )

            for order in orders:
                order.batch = batch

            Order.objects.bulk_update(orders, ['batch'])

            BatchingService._emit_batch_created_event(batch, orders)

            return batch

        return None

    @staticmethod
    def _emit_batch_created_event(batch, orders):
        """Emit domain event when batch is created."""
        try:
            from domain.events.bus import EventBus
            from domain.events.types import BatchCreated

            EventBus.publish(BatchCreated(
                aggregate_id=str(batch.id),
                payload={
                    'batch_type': batch.batch_type,
                    'order_count': len(orders),
                    'order_ids': [str(o.id) for o in orders],
                    'restaurant_id': str(batch.restaurant_id)
                }
            ))
        except Exception as e:
            logger.warning(f"Failed to emit BatchCreated event: {e}")
