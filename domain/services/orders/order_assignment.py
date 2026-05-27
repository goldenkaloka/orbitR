import logging
from django.db import transaction
from apps.orders.models import Order, DeliveryBatch
from apps.delivery.models import Driver, DeliveryAssignment
from domain.fsm.order_fsm import OrderFSM
from domain.fsm.driver_fsm import DriverFSM
from domain.events.bus import EventBus
from domain.events.types import DriverAssigned
from apps.events.utils import broadcast_order_status

logger = logging.getLogger(__name__)

class OfferOrderService:
    @staticmethod
    def execute(order_id, driver_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if order.driver:
                if order.driver_id == driver_id:
                    return order
                raise ValueError("Order already assigned or offered.")
            
            driver = Driver.objects.select_for_update().get(id=driver_id)
            new_state = OrderFSM.transition(order.status, 'OFFERED')
            order.status = new_state
            order.driver = driver
            order.save()
            
            transaction.on_commit(lambda: broadcast_order_status(str(order.id), 'OFFERED'))
            return order

class AcceptOrderService:
    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if order.status != 'OFFERED':
                raise ValueError(f"Invalid state: {order.status}")
            if order.driver.user != actor:
                raise PermissionError("Not your offer.")
            return AssignDriverService.execute(order.id, order.driver.id, actor)

class RejectOrderService:
    @staticmethod
    def execute(order_id, actor):
        """
        Driver rejects/declines an offered order.

        If the order belongs to a batch (the normal dispatch path), the entire
        batch must be declined via BatchDispatchService so that:
          - DriverFreeService atomically releases the driver
          - All orders in the batch are reset to READY
          - The batch is re-queued for dispatch to the next driver
          - offer_attempt is incremented correctly

        For legacy single-order offers (no batch), fall back to the original
        per-order reset path.
        """
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)

            if order.status != 'OFFERED':
                raise ValueError(f"Invalid state: {order.status}")
            if not order.driver or order.driver.user != actor:
                raise PermissionError("Not your offer.")

            # ── Batch path ────────────────────────────────────────────────────
            # When the order is part of a batch, decline the whole batch.
            # This is the correct path for all system-dispatched offers.
            if order.batch_id:
                driver = order.driver
                batch = DeliveryBatch.objects.select_for_update().get(id=order.batch_id)

                if batch.status != 'OFFERED':
                    raise ValueError(
                        f"Batch {order.batch_id} is in '{batch.status}' state — cannot decline."
                    )
                if batch.driver_id != driver.id:
                    raise PermissionError("This batch was not offered to you.")

                # Delegate to BatchDispatchService which handles DriverFreeService,
                # attempt counting, re-dispatch, and escalation correctly.
                from domain.services.batch_dispatch_service import BatchDispatchService
                # Release the transaction lock before calling decline_batch which
                # opens its own atomic block internally.
                # We must save nothing here — decline_batch owns all state changes.
                return BatchDispatchService.decline_batch(
                    str(order.batch_id), str(driver.id), actor
                )

            # ── Legacy single-order path ──────────────────────────────────────
            # Orders offered without a batch (manual dispatch / legacy flow).
            driver = order.driver
            order.status = OrderFSM.transition(order.status, 'READY')
            order.driver = None
            order.save()

            driver.total_claim_attempts += 1
            driver.save(update_fields=['total_claim_attempts'])

            from domain.services.dispatch_tasks import dispatch_order_task
            transaction.on_commit(lambda: dispatch_order_task.delay(str(order.id)))
            return order

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
            
            if driver.status != 'ASSIGNED':
                driver.status = DriverFSM.transition(driver.status, 'ASSIGNED')
            driver.current_active_orders += 1
            driver.save(update_fields=['status', 'current_active_orders'])
            
            DeliveryAssignment.objects.update_or_create(order=order, defaults={'driver': driver})
            
            transaction.on_commit(lambda: EventBus.publish(DriverAssigned(aggregate_id=order.id, payload={'driver_id': str(driver.id), 'actor_id': str(actor.id)})))
            transaction.on_commit(lambda: broadcast_order_status(str(order.id), 'ASSIGNED'))
            return order

class AssignBatchService:
    @staticmethod
    def execute(batch_id, driver_id, actor):
        from django.db.models import Count

        with transaction.atomic():
            batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
            if batch.status != 'CREATED':
                logger.warning(f"Batch {batch_id} is in {batch.status} state, cannot assign.")
                return batch

            driver = Driver.objects.select_for_update().get(id=driver_id)

            if driver.driver_type == 'temporary':
                if batch.batch_type == 'CROSS_RESTAURANT':
                    raise PermissionError("Temporary drivers cannot accept cross-restaurant batches.")
                first_order = batch.orders.first()
                if first_order and driver.assigned_by_id != first_order.restaurant_id:
                    raise PermissionError("Temporary driver can only take orders from assigned restaurant.")

            orders = batch.orders.all()
            new_order_count = orders.count()
            current_load = driver.assignments.filter(delivered_at__isnull=True).count()

            if current_load + new_order_count > driver.max_active_orders:
                raise ValueError(
                    f"Driver cannot accept {new_order_count} more orders. "
                    f"Current: {current_load}, Max: {driver.max_active_orders}"
                )

            batch.status = 'ASSIGNED'
            batch.driver = driver
            batch.save()

            if driver.status != 'ASSIGNED':
                driver.status = DriverFSM.transition(driver.status, 'ASSIGNED')

            driver.current_active_orders += new_order_count
            driver.save(update_fields=['status', 'current_active_orders'])

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

