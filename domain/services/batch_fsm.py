from __future__ import annotations

"""
BatchFSM — Dispatch-only Finite State Machine for DeliveryBatch.

Batch tracks only the driver-hunting lifecycle. Fulfillment tracking
(ASSIGNED, PICKED, COMPLETED) has moved to orders — batch never stores
those states. Once a batch is DISPATCHED, its job is done.

States:
    CREATED          — Batch created, waiting for restaurant to assign driver
    PENDING_DRIVER   — Restaurant notified, waiting for them to pick a driver
    DISPATCHED       — Driver assigned (terminal for dispatch)
    CANCELLED        — Batch cancelled by timeout or admin

Events:
    WAIT_FOR_DRIVER  — Move from CREATED to PENDING_DRIVER
    ACCEPT           — Restaurant assigns a driver
    REASSIGN         — Restaurant replaces the driver (DISPATCHED → DISPATCHED)
    TIMEOUT          — No driver assigned within time limit
    CANCEL           — Admin or system cancels the batch
"""
import logging

logger = logging.getLogger(__name__)


class BatchFSMError(Exception):
    """Raised when an invalid batch state transition is attempted."""
    pass


class BatchFSM:
    TRANSITIONS: dict[tuple, str] = {
        ('CREATED',          'WAIT_FOR_DRIVER'): 'PENDING_DRIVER',
        ('CREATED',          'ACCEPT'):          'DISPATCHED',
        ('PENDING_DRIVER',   'ACCEPT'):          'DISPATCHED',
        ('PENDING_DRIVER',   'TIMEOUT'):         'CANCELLED',
        ('CREATED',          'CANCEL'):          'CANCELLED',
        ('PENDING_DRIVER',   'CANCEL'):          'CANCELLED',
        ('DISPATCHED',       'CANCEL'):          'CANCELLED',
        ('DISPATCHED',       'REASSIGN'):        'DISPATCHED',
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
        return batch.status in ('CREATED',)
