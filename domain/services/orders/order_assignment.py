import logging
from django.db import transaction
from apps.orders.models import Order, DeliveryBatch
from apps.delivery.models import Driver, DeliveryAssignment
from domain.fsm.order_fsm import OrderFSM
from domain.fsm.driver_fsm import DriverFSM
from domain.services.batch_fsm import BatchFSM
from domain.events.bus import EventBus
from domain.events.types import DriverAssigned
from apps.events.utils import broadcast_order_status

logger = logging.getLogger(__name__)


class RejectOrderService:
    """
    Driver rejects/declines an order that belongs to an OFFERED batch.
    Delegates to BatchDispatchService to handle the batch-level lifecycle.
    """

    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)

            if not order.batch_id:
                raise ValueError("Order is not part of any batch.")

            batch = DeliveryBatch.objects.select_for_update().get(id=order.batch_id)

            # If the offer window already expired, the batch will be back in CREATED.
            # Treat this as a no-op — the expiry handler already did the work.
            if batch.status == 'CREATED':
                logger.info(
                    f"[RejectOrderService] Batch {order.batch_id} is already CREATED "
                    f"(offer likely expired before driver responded). Ignoring."
                )
                return order

            if batch.status != 'OFFERED':
                raise ValueError(
                    f"Batch {order.batch_id} is in '{batch.status}' state — cannot decline."
                )

            driver = Driver.objects.select_for_update().get(user=actor)
            if str(batch.driver_id) != str(driver.id):
                raise PermissionError("This batch was not offered to you.")

            from domain.services.batch_dispatch_service import BatchDispatchService
            return BatchDispatchService.decline_batch(
                str(order.batch_id), str(driver.id), actor
            )


class AssignDriverService:
    @staticmethod
    def execute(order_id, driver_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if order.driver and order.driver_id != driver_id:
                raise ValueError("Order already assigned to someone else.")

            driver = Driver.objects.select_for_update().get(id=driver_id)
            if driver.driver_type == 'temporary' and driver.assigned_by_id != order.restaurant_id:
                raise PermissionError("Temporary drivers restricted to their restaurant.")

            order.status = OrderFSM.transition(order.status, 'ASSIGNED')
            order.driver = driver
            order.save()

            if driver.status != 'DELIVERING':
                driver.status = DriverFSM.transition(driver.status, 'DELIVERING')
            driver.current_active_orders += 1
            driver.save(update_fields=['status', 'current_active_orders'])

            DeliveryAssignment.objects.update_or_create(order=order, defaults={'driver': driver})

            transaction.on_commit(lambda: EventBus.publish(DriverAssigned(
                aggregate_id=order.id,
                payload={'driver_id': str(driver.id), 'actor_id': str(actor.id)}
            )))
            transaction.on_commit(lambda: broadcast_order_status(str(order.id), 'ASSIGNED'))
            return order


class AssignBatchService:
    @staticmethod
    def execute(batch_id, driver_id, actor):
        from django.db.models import Count

        with transaction.atomic():
            batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)

            if batch.status == 'WAITING_TEMP_DRIVER':
                # Expired batch — don't touch batch state, return early so
                # caller falls through to AssignDriverService for single-order claim.
                logger.info(f"Batch {batch_id} is WAITING_TEMP_DRIVER — returning for fallback.")
                return batch

            if batch.status not in ('CREATED', 'OFFERED'):
                logger.warning(f"Batch {batch_id} is in {batch.status} state, cannot assign.")
                return batch

            if batch.status == 'OFFERED' and str(batch.driver_id) != str(driver_id):
                logger.warning(f"Batch {batch_id} is offered to another driver, cannot assign.")
                return batch

            driver = Driver.objects.select_for_update().get(id=driver_id)

            if driver.driver_type == 'temporary':
                if batch.batch_type == 'CROSS_RESTAURANT':
                    raise PermissionError("Temporary drivers cannot accept cross-restaurant batches.")
                first_order = batch.orders.first()
                if first_order and driver.assigned_by_id != first_order.restaurant_id:
                    raise PermissionError("Temporary driver can only take orders from assigned restaurant.")

            orders = list(batch.orders.all())
            new_order_count = len(orders)
            current_load = driver.assignments.filter(delivered_at__isnull=True).count()

            if current_load + new_order_count > driver.max_active_orders:
                raise ValueError(
                    f"Driver cannot accept {new_order_count} more orders. "
                    f"Current: {current_load}, Max: {driver.max_active_orders}"
                )

            batch.status = BatchFSM.transition(batch, 'ACCEPT')
            batch.driver = driver
            batch.save()

            if driver.status != 'DELIVERING':
                driver.status = DriverFSM.transition(driver.status, 'DELIVERING')
            driver.current_active_orders += new_order_count
            driver.total_claims_accepted += 1
            driver.save(update_fields=['status', 'current_active_orders', 'total_claims_accepted'])

            for order in orders:
                order.status = OrderFSM.transition(order.status, 'ASSIGNED')
                order.driver = driver
                order.save()

                DeliveryAssignment.objects.update_or_create(
                    order=order,
                    defaults={'driver': driver}
                )

                transaction.on_commit(lambda o=order: EventBus.publish(DriverAssigned(
                    aggregate_id=o.id,
                    payload={'driver_id': str(driver.id), 'batch_id': str(batch.id), 'actor_id': str(actor.id)}
                )))

            return batch
