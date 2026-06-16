import logging
from django.db import transaction
from apps.orders.models import Order, DeliveryBatch
from apps.delivery.models import Driver, DeliveryAssignment
from domain.fsm.order_fsm import OrderFSM
from domain.fsm.driver_fsm import DriverFSM
from domain.services.batch_fsm import BatchFSM
from domain.services.driver_service import release_driver
from domain.services.permissions import check_restaurant_permission, can_serve_restaurant, can_driver_delegate
from domain.events.bus import EventBus
from domain.events.types import DriverAssigned
from apps.events.utils import broadcast_order_status

logger = logging.getLogger(__name__)


class AssignDriverService:
    """
    Single-order driver assignment (fallback path).
    Used by the driver claim-job flow and temp-driver registration
    when there is no batch or batching is not applicable.
    """

    @staticmethod
    def execute(order_id, driver_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if order.driver and order.driver_id != driver_id:
                raise ValueError("Order already assigned to someone else.")

            driver = Driver.objects.select_for_update().get(id=driver_id)
            if not can_serve_restaurant(driver, order.restaurant_id):
                raise PermissionError("Driver cannot serve this restaurant.")

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
    Restaurant assigns (or reassigns) a driver to a batch.
    First assignment: CREATED/PENDING_DRIVER → DISPATCHED.
    Reassignment: DISPATCHED → DISPATCHED (releases old driver first).
    """

    @staticmethod
    def execute(batch_id, driver_id, actor):
        with transaction.atomic():
            batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)

            is_dispatch = batch.status in ('CREATED', 'PENDING_DRIVER')
            is_reassign = batch.status == 'DISPATCHED'

            if not is_dispatch and not is_reassign:
                raise ValueError(
                    f"Batch {batch_id} is in '{batch.status}' state — "
                    "expected CREATED, PENDING_DRIVER, or DISPATCHED."
                )

            # --- Race condition guard: prevent silent overwrite ---
            if is_reassign and batch.driver and str(batch.driver.id) != str(driver_id):
                raise ValueError(
                    f"Batch {batch_id} already has driver '{batch.driver.user.username}' assigned. "
                    "Refresh and confirm reassignment if you want to change drivers."
                )

            # All sections must be READY before driver can be assigned
            unready = batch.section_statuses.exclude(status='READY').exists()
            if unready:
                raise ValueError(
                    f"Batch {batch_id} has sections not yet marked READY — "
                    "cannot assign driver."
                )

            if not check_restaurant_permission(actor, batch.restaurant) and not can_driver_delegate(actor, batch):
                raise PermissionError("You are not authorized to assign drivers for this restaurant's batches.")

            driver = Driver.objects.select_for_update().get(id=driver_id)

            # Verify driver can serve this restaurant
            first_order = batch.orders.first()
            if first_order and not can_serve_restaurant(driver, first_order.restaurant_id):
                raise PermissionError("Driver cannot serve this restaurant.")

            orders = list(batch.orders.all())
            new_order_count = len(orders)

            # --- Capacity check uses new_order_count (not unique_stops) to match the increment ---
            current_load = driver.assignments.filter(delivered_at__isnull=True).count()

            if current_load + new_order_count > driver.max_active_orders:
                raise ValueError(
                    f"Driver cannot take {new_order_count} more orders. "
                    f"Current active assignments: {current_load}, Max: {driver.max_active_orders}"
                )

            # --- Release old driver if reassigning ---
            if is_reassign and batch.driver:
                old_driver = Driver.objects.select_for_update().get(id=batch.driver.id)
                release_driver(old_driver, new_order_count)
                DeliveryAssignment.objects.filter(order__in=orders).delete()

            # --- Assign new driver ---
            event = 'REASSIGN' if is_reassign else 'ACCEPT'
            batch.status = BatchFSM.transition(batch, event)
            batch.driver = driver
            batch.save(update_fields=['status', 'driver'])

            # Handle OFFLINE driver: transition OFFLINE → AVAILABLE → DELIVERING
            if driver.status == 'OFFLINE':
                driver.status = DriverFSM.transition(driver.status, 'AVAILABLE')
            if driver.status != 'DELIVERING':
                driver.status = DriverFSM.transition(driver.status, 'DELIVERING')
            driver.current_active_orders += new_order_count
            driver.save(update_fields=['status', 'current_active_orders'])

            for order in orders:
                if is_reassign:
                    order.status = 'ASSIGNED'
                else:
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
