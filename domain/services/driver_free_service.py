"""
DriverFreeService — Single, authoritative point for releasing a driver from a batch.

Fixes bugs #1 and #2: driver reference was cleared BEFORE being used,
so driver stats were never decremented and driver was permanently locked.

Rule: ALWAYS use this service when a driver needs to be freed from a batch,
whether due to decline, timeout, or cancellation.
"""
import logging
from django.db import transaction

logger = logging.getLogger(__name__)


class DriverFreeService:
    """
    Releases a driver from their batch obligation atomically.

    Handles:
    - Decrementing driver.current_active_orders
    - Resetting driver status to AVAILABLE via DriverFSM
    - Clearing batch.driver
    - Resetting all batch orders to READY with no driver
    - Logging the reason

    This is the ONLY place where these operations should occur.
    Never clear batch.driver manually elsewhere.
    """

    @staticmethod
    @transaction.atomic
    def release(batch_id: str, event: str, reason: str = ""):
        """
        Release the driver assigned to a batch.

        Args:
            batch_id: UUID string of the DeliveryBatch
            event: BatchFSM event to trigger ('DECLINE', 'TIMEOUT', 'CANCEL')
            reason: Human-readable reason for logging
        """
        from apps.orders.models import DeliveryBatch, Order
        from domain.services.batch_fsm import BatchFSM, BatchFSMError
        from domain.fsm.driver_fsm import DriverFSM
        from apps.delivery.models import Driver

        try:
            batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
        except DeliveryBatch.DoesNotExist:
            logger.error(f"[DriverFreeService] Batch {batch_id} not found")
            return

        # ─── CRITICAL FIX ───────────────────────────────────────────────────
        # Save the driver reference BEFORE clearing it on the batch object.
        # The old code did `batch.driver = None` then `if batch.driver:` which
        # always evaluates to False, permanently leaking the driver's state.
        # ─────────────────────────────────────────────────────────────────────
        current_driver_id = batch.driver_id  # Use the FK id, never the object
        was_accepted = batch.status in ('ASSIGNED', 'PICKED', 'COMPLETED')

        order_count = batch.orders.count()

        # Apply FSM transition
        try:
            new_status = BatchFSM.transition(batch, event)
        except BatchFSMError as e:
            logger.warning(f"[DriverFreeService] FSM error releasing batch {batch_id}: {e}")
            # Still attempt to free the driver even if transition fails
            new_status = 'CREATED'

        batch.driver = None
        batch.status = new_status
        batch.save(update_fields=['driver', 'status'])

        # Reset orders
        Order.objects.filter(batch_id=batch_id).update(
            status='READY',
            driver=None
        )

        # Free the driver if one was assigned
        if current_driver_id:
            try:
                driver = Driver.objects.select_for_update().get(id=current_driver_id)

                # Only decrement capacity if the driver had actually accepted
                # (current_active_orders is incremented only on accept_batch, not on offer)
                if was_accepted:
                    driver.current_active_orders = max(
                        0, driver.current_active_orders - order_count
                    )

                try:
                    driver.status = DriverFSM.transition(driver.status, 'AVAILABLE')
                except Exception as fsm_err:
                    # Driver FSM might reject AVAILABLE from current state —
                    # force it to AVAILABLE as a safety net
                    logger.warning(
                        f"[DriverFreeService] DriverFSM rejected AVAILABLE for driver "
                        f"{current_driver_id} (status={driver.status}): {fsm_err}. Forcing."
                    )
                    driver.status = 'AVAILABLE'

                driver.save(update_fields=['status', 'current_active_orders'])

                logger.info(
                    f"[DriverFreeService] Released driver {current_driver_id} "
                    f"from batch {batch_id}. Reason: {reason or event}. "
                    f"Orders reset: {order_count}"
                )

            except Driver.DoesNotExist:
                logger.error(
                    f"[DriverFreeService] Driver {current_driver_id} not found "
                    f"while releasing batch {batch_id}"
                )
        else:
            logger.info(
                f"[DriverFreeService] Batch {batch_id} had no driver assigned. "
                f"Orders reset: {order_count}. Reason: {reason or event}"
            )
