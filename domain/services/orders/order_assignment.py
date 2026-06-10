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


class AssignDriverService:
    @staticmethod
    def execute(order_id, driver_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if order.driver and order.driver_id != driver_id:
                raise ValueError("Order already assigned to someone else.")

            driver = Driver.objects.select_for_update().get(id=driver_id)
            if driver.assigned_by_id != order.restaurant_id:
                raise PermissionError("Driver not assigned to this restaurant.")

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
    """
    Restaurant assigns a driver to a PENDING_DRIVER batch.
    Transitions batch to DISPATCHED and all orders to ASSIGNED.
    """

    @staticmethod
    def execute(batch_id, driver_id, actor):
        with transaction.atomic():
            batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)

            if batch.status not in ('CREATED', 'PENDING_DRIVER'):
                raise ValueError(
                    f"Batch {batch_id} is in '{batch.status}' state — "
                    "expected CREATED or PENDING_DRIVER."
                )

            driver = Driver.objects.select_for_update().get(id=driver_id)

            # Verify driver belongs to the same restaurant as the batch orders
            first_order = batch.orders.first()
            if first_order and driver.assigned_by_id != first_order.restaurant_id:
                raise PermissionError("Driver can only take orders from their assigned restaurant.")

            orders = list(batch.orders.all())
            new_order_count = len(orders)
            current_load = driver.assignments.filter(delivered_at__isnull=True).count()

            if current_load + new_order_count > driver.max_active_orders:
                raise ValueError(
                    f"Driver cannot take {new_order_count} more orders. "
                    f"Current: {current_load}, Max: {driver.max_active_orders}"
                )

            batch.status = BatchFSM.transition(batch, 'ACCEPT')
            batch.driver = driver
            batch.save(update_fields=['status', 'driver'])

            if driver.status != 'DELIVERING':
                driver.status = DriverFSM.transition(driver.status, 'DELIVERING')
            driver.current_active_orders += new_order_count
            driver.save(update_fields=['status', 'current_active_orders'])

            for order in orders:
                order.status = OrderFSM.transition(order.status, 'ASSIGNED')
                order.driver = driver
                order.save(update_fields=['status', 'driver'])

                DeliveryAssignment.objects.update_or_create(
                    order=order,
                    defaults={'driver': driver}
                )

                transaction.on_commit(lambda o=order: EventBus.publish(DriverAssigned(
                    aggregate_id=o.id,
                    payload={'driver_id': str(driver.id), 'batch_id': str(batch.id), 'actor_id': str(actor.id)}
                )))

            return batch
