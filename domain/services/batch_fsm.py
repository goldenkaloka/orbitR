"""
BatchFSM — Full lifecycle Finite State Machine for DeliveryBatch.

States:
    ACCUMULATING   — Collecting orders. New orders can join.
    SOFT_LOCK      — Last 60s. Only compatible orders may join.
    FINALIZING     — Closed to new orders. Waiting for sections to finish.
    PRE_ASSIGNING  — 80%+ sections ready. Driver pre-warm.
    PENDING_DRIVER — All sections ready. Restaurant assigns driver.
    DISPATCHED     — Driver assigned. En route.
    DELIVERED      — All orders delivered (terminal).
    PARTIAL_BATCH  — Some sections cancelled, remainder proceeding.
    CANCELLED      — Cancelled (terminal).
    EXPIRED        — Expired (terminal).

Events:
    ADD_ORDER       — Add order to ACCUMULATING batch
    JOIN_SOFT       — Add order to SOFT_LOCK batch (only compatible)
    TIMER_SOFT_LOCK — ACCUMULATING → SOFT_LOCK (timer or max orders)
    TIMER_FINALIZE  — SOFT_LOCK → FINALIZING (timer expired or no joins)
    SECTION_READY   — A section became READY
    ALL_SECTIONS_READY — All sections READY → PENDING_DRIVER
    THRESHOLD_PRE_ASSIGN — 80% sections ready → PRE_ASSIGNING
    ACCEPT          — Restaurant assigns a driver
    REASSIGN        — Restaurant replaces the driver
    TIMEOUT         — No driver assigned within time limit
    CANCEL          — Admin or system cancels the batch
    SECTION_FAIL    — A section failed → PARTIAL_BATCH
    ALL_DELIVERED   — All orders delivered
"""
import logging

logger = logging.getLogger(__name__)


class BatchFSMError(Exception):
    """Raised when an invalid batch state transition is attempted."""
    pass


class BatchFSM:
    TRANSITIONS: dict[tuple, str] = {
        # ── Forward lifecycle ────────────────────────────────────────────────
        ('ACCUMULATING',    'ADD_ORDER'):            'ACCUMULATING',
        ('ACCUMULATING',    'TIMER_SOFT_LOCK'):      'SOFT_LOCK',
        ('ACCUMULATING',    'ALL_SECTIONS_READY'):   'PENDING_DRIVER',
        ('ACCUMULATING',    'CANCEL'):               'CANCELLED',
        ('ACCUMULATING',    'SECTION_FAIL'):         'PARTIAL_BATCH',

        ('SOFT_LOCK',       'JOIN_SOFT'):            'SOFT_LOCK',
        ('SOFT_LOCK',       'TIMER_FINALIZE'):       'FINALIZING',
        ('SOFT_LOCK',       'ALL_SECTIONS_READY'):   'PENDING_DRIVER',
        ('SOFT_LOCK',       'CANCEL'):               'CANCELLED',
        ('SOFT_LOCK',       'SECTION_FAIL'):         'PARTIAL_BATCH',

        ('FINALIZING',      'SECTION_READY'):        'FINALIZING',
        ('FINALIZING',      'THRESHOLD_PRE_ASSIGN'): 'PRE_ASSIGNING',
        ('FINALIZING',      'ALL_SECTIONS_READY'):   'PENDING_DRIVER',
        ('FINALIZING',      'CANCEL'):               'CANCELLED',
        ('FINALIZING',      'SECTION_FAIL'):         'PARTIAL_BATCH',

        ('PRE_ASSIGNING',   'SECTION_READY'):        'PRE_ASSIGNING',
        ('PRE_ASSIGNING',   'ALL_SECTIONS_READY'):   'PENDING_DRIVER',
        ('PRE_ASSIGNING',   'CANCEL'):               'CANCELLED',
        ('PRE_ASSIGNING',   'SECTION_FAIL'):         'PARTIAL_BATCH',

        ('PENDING_DRIVER',  'ACCEPT'):               'DISPATCHED',
        ('PENDING_DRIVER',  'TIMEOUT'):              'EXPIRED',
        ('PENDING_DRIVER',  'CANCEL'):               'CANCELLED',

        ('DISPATCHED',      'REASSIGN'):             'DISPATCHED',
        ('DISPATCHED',      'CANCEL'):               'CANCELLED',
        ('DISPATCHED',      'ALL_DELIVERED'):        'DELIVERED',

        # A batch whose orders have all reached a terminal state is finished
        # even if it never reached DISPATCHED (e.g. single-order assignment
        # bypassed the batch, or every order was cancelled).
        ('ACCUMULATING',    'ALL_DELIVERED'):        'DELIVERED',
        ('SOFT_LOCK',       'ALL_DELIVERED'):        'DELIVERED',
        ('FINALIZING',      'ALL_DELIVERED'):        'DELIVERED',
        ('PRE_ASSIGNING',   'ALL_DELIVERED'):        'DELIVERED',
        ('PENDING_DRIVER',  'ALL_DELIVERED'):        'DELIVERED',
        ('PARTIAL_BATCH',   'ALL_DELIVERED'):        'DELIVERED',

        ('PARTIAL_BATCH',   'SECTION_READY'):        'PARTIAL_BATCH',
        ('PARTIAL_BATCH',   'ALL_SECTIONS_READY'):   'PENDING_DRIVER',
        ('PARTIAL_BATCH',   'CANCEL'):               'CANCELLED',
        ('PARTIAL_BATCH',   'SECTION_FAIL'):         'PARTIAL_BATCH',
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
    def can_accept_orders(cls, batch) -> bool:
        """Returns True if the batch can still accept new orders."""
        return batch.status in ('ACCUMULATING', 'SOFT_LOCK')

    @classmethod
    def can_dispatch(cls, batch) -> bool:
        """Returns True if the batch is in a state where dispatch can be attempted."""
        return batch.status in ('PENDING_DRIVER',)

    @classmethod
    def can_pre_assign(cls, batch) -> bool:
        """Returns True if the batch can enter pre-assignment."""
        return batch.status in ('FINALIZING',)

    @classmethod
    def sections_all_ready(cls, batch) -> bool:
        """Check if all sections for this batch are READY."""
        return not batch.section_statuses.exclude(status='READY').exists()

    @classmethod
    def sections_ready_pct(cls, batch) -> float:
        """Return the percentage of sections that are READY (0.0 – 1.0)."""
        total = batch.section_statuses.count()
        if total == 0:
            return 1.0
        ready = batch.section_statuses.filter(status='READY').count()
        return ready / total
