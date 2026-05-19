from __future__ import annotations

"""
BatchFSM — Explicit Finite State Machine for DeliveryBatch.

All batch status transitions MUST go through BatchFSM.transition().
Invalid transitions raise BatchFSMError immediately — no silent failures.
"""
import logging

logger = logging.getLogger(__name__)


class BatchFSMError(Exception):
    """Raised when an invalid batch state transition is attempted."""
    pass


class BatchFSM:
    """
    Defines every valid batch lifecycle transition.

    States:
        CREATED          — Batch created, waiting for a driver offer
        OFFERED          — Offer sent to a specific driver
        ASSIGNED         — Driver accepted
        PICKED_UP        — Driver picked up all orders
        DELIVERED        — All orders delivered
        RETRYING         — Offer declined/timed out, looking for next driver
        WAITING_TEMP_DRIVER — Max permanent-driver attempts exhausted
        CANCELLED        — Batch cancelled (no driver found, or orders failed)
        EXPIRED          — System-level expiry after max attempts

    Events:
        OFFER            — Dispatch sends offer to a driver
        ACCEPT           — Driver accepts the offer
        DECLINE          — Driver declines the offer
        TIMEOUT          — Offer window expired (5 minutes)
        PICKUP           — Driver confirmed pickup at restaurant
        DELIVER          — Driver confirmed final delivery
        MAX_ATTEMPTS     — Max offer attempts exhausted
        TEMP_ASSIGNED    — A temporary/restaurant driver accepts
        CANCEL           — Admin or system cancels the batch
    """

    TRANSITIONS: dict[tuple, str] = {
        # Normal dispatch flow
        ('CREATED',             'OFFER'):         'OFFERED',
        ('OFFERED',             'ACCEPT'):        'ASSIGNED',
        ('OFFERED',             'DECLINE'):       'CREATED',
        ('OFFERED',             'TIMEOUT'):       'CREATED',
        ('ASSIGNED',            'PICKUP'):        'PICKED_UP',
        ('PICKED_UP',           'DELIVER'):       'DELIVERED',

        # Retry / fallback flow
        ('CREATED',             'MAX_ATTEMPTS'):  'EXPIRED',
        ('OFFERED',             'MAX_ATTEMPTS'):  'EXPIRED',
        ('EXPIRED',             'ESCALATE'):      'WAITING_TEMP_DRIVER',
        ('WAITING_TEMP_DRIVER', 'TEMP_ASSIGNED'): 'ASSIGNED',
        ('WAITING_TEMP_DRIVER', 'TIMEOUT'):       'CANCELLED',

        # Cancellation — allowed from most active states
        ('CREATED',             'CANCEL'):        'CANCELLED',
        ('OFFERED',             'CANCEL'):        'CANCELLED',
        ('ASSIGNED',            'CANCEL'):        'CANCELLED',
        ('WAITING_TEMP_DRIVER', 'CANCEL'):        'CANCELLED',
    }

    @classmethod
    def transition(cls, batch, event: str) -> str:
        """
        Compute the next state for a batch given an event.

        Args:
            batch: DeliveryBatch instance (must have .status attribute)
            event: One of the defined event strings above

        Returns:
            New status string

        Raises:
            BatchFSMError: If the transition is not defined
        """
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
    def is_terminal(cls, status: str) -> bool:
        """Returns True if the status is a terminal state (no further transitions)."""
        return status in ('DELIVERED', 'CANCELLED', 'EXPIRED')

    @classmethod
    def can_dispatch(cls, batch) -> bool:
        """Returns True if the batch is in a state where dispatch can be attempted."""
        return batch.status == 'CREATED'
