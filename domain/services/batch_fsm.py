from __future__ import annotations

"""
BatchFSM — Dispatch-only Finite State Machine for DeliveryBatch.

Batch tracks only the driver-hunting lifecycle. Fulfillment tracking
(ASSIGNED, PICKED, COMPLETED) has moved to orders — batch never stores
those states. Once a batch is DISPATCHED, its job is done.
"""
import logging

logger = logging.getLogger(__name__)


class BatchFSMError(Exception):
    """Raised when an invalid batch state transition is attempted."""
    pass


class BatchFSM:
    """
    Defines every valid batch dispatch transition.

    States:
        CREATED              — Batch created, ready for dispatch
        OFFERED              — Offer sent to a specific driver
        EXPIRED              — Max offer attempts reached
        WAITING_TEMP_DRIVER  — Escalated: max permanent-driver attempts exhausted
        DISPATCHED           — Driver found and assigned (terminal for dispatch)
        CANCELLED            — Batch cancelled by admin / temp-driver timeout

    Events:
        OFFER       — Dispatch sends offer to a driver
        ACCEPT      — Driver accepts the offer / claims the batch
        DECLINE     — Driver declines the offer
        TIMEOUT     — Offer window expired
        MAX_ATTEMPTS — Max offer attempts exhausted
        ESCALATE    — Move from EXPIRED to WAITING_TEMP_DRIVER
        CANCEL      — Admin or system cancels the batch
    """

    TRANSITIONS: dict[tuple, str] = {
        # Normal dispatch flow
        ('CREATED',  'OFFER'):   'OFFERED',
        ('CREATED',  'ACCEPT'):  'DISPATCHED',       # direct claim via claim-job
        ('OFFERED',  'ACCEPT'):  'DISPATCHED',
        ('OFFERED',  'DECLINE'): 'CREATED',           # decline → retry
        ('OFFERED',  'TIMEOUT'): 'CREATED',           # timeout → retry

        # Retry / fallback
        ('CREATED',  'MAX_ATTEMPTS'): 'EXPIRED',
        ('OFFERED',  'MAX_ATTEMPTS'): 'EXPIRED',
        ('EXPIRED',  'ESCALATE'):     'WAITING_TEMP_DRIVER',

        # Temp-driver → driver found
        ('WAITING_TEMP_DRIVER', 'ACCEPT'): 'DISPATCHED',
        ('WAITING_TEMP_DRIVER', 'TIMEOUT'): 'CANCELLED',

        # Cancellation — allowed from any active state
        ('CREATED',              'CANCEL'): 'CANCELLED',
        ('OFFERED',              'CANCEL'): 'CANCELLED',
        ('WAITING_TEMP_DRIVER',  'CANCEL'): 'CANCELLED',
        ('DISPATCHED',           'CANCEL'): 'CANCELLED',
        ('EXPIRED',              'CANCEL'): 'CANCELLED',
    }

    @classmethod
    def transition(cls, batch, event: str) -> str:
        current = batch.status
        key = (current, event)
        new_status = cls.TRANSITIONS.get(key)

        if not new_status:
            raise BatchFSMError(
                f"Invalid batch transition: status='{current}' + event='{event}'. "
                f"Batch ID: {batch.id}"
            )

        logger.debug(
            f"[BatchFSM] Batch {batch.id}: {current} → {new_status} (event={event})"
        )
        return new_status

    @classmethod
    def can_dispatch(cls, batch) -> bool:
        """Returns True if the batch is in a state where dispatch can be attempted."""
        return batch.status == 'CREATED'
