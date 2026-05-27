class InvalidPoolTransitionError(Exception):
    pass


class PoolFSM:
    """
    State machine for OrderPool lifecycle.
    
    States:
      OPEN        — Members join and add items
      LOCKED      — Initiator locked, members pay their shares
      PAID        — All members have paid, restaurant starts preparing
      READY       — Restaurant marked all items ready, batching begins
      DISPATCHING — Batch created, driver search in progress
      DELIVERING  — Driver picked up, en route
      COMPLETED   — All items delivered
      CANCELLED   — Cancelled by initiator/admin (before payment or with refunds)
      EXPIRED     — Timeout reached
      REFUNDED    — Full refund processed after cancellation
    """
    STATES = [
        'OPEN', 'LOCKED', 'PAID', 'READY',
        'DISPATCHING', 'DELIVERING', 'COMPLETED',
        'CANCELLED', 'EXPIRED', 'REFUNDED',
    ]

    ALLOWED_TRANSITIONS = {
        'OPEN':   ['LOCKED', 'CANCELLED', 'EXPIRED'],
        'LOCKED': ['PAID', 'CANCELLED', 'EXPIRED'],
        'PAID':   ['READY', 'CANCELLED', 'EXPIRED'],
        'READY':  ['DISPATCHING', 'CANCELLED'],
        'DISPATCHING': ['DELIVERING', 'EXPIRED', 'CANCELLED'],
        'DELIVERING':  ['COMPLETED', 'CANCELLED'],
        'COMPLETED':   [],
        'CANCELLED':   ['REFUNDED'],
        'EXPIRED':     ['CANCELLED', 'REFUNDED'],
        'REFUNDED':    [],
    }

    @staticmethod
    def transition(current_state: str, next_state: str) -> str:
        if current_state not in PoolFSM.STATES:
            raise InvalidPoolTransitionError(f"Invalid current state: {current_state}")
        if next_state not in PoolFSM.STATES:
            raise InvalidPoolTransitionError(f"Invalid next state: {next_state}")
        if next_state not in PoolFSM.ALLOWED_TRANSITIONS.get(current_state, []):
            raise InvalidPoolTransitionError(
                f"Cannot transition pool from {current_state} to {next_state}"
            )
        return next_state

    @staticmethod
    def can_lock(status: str) -> bool:
        return status == 'OPEN'

    @staticmethod
    def can_cancel(status: str) -> bool:
        return status in ('OPEN', 'LOCKED', 'PAID')

    @staticmethod
    def can_mark_ready(status: str) -> bool:
        return status == 'PAID'

    @staticmethod
    def can_dispatch(status: str) -> bool:
        return status == 'READY'
