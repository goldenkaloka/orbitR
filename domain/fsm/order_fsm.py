class InvalidStateTransitionError(Exception):
    pass

class OrderFSM:
    STATES = [
        'PENDING_PAYMENT',
        'PENDING_MANUAL_ASSIGNMENT',
        'CREATED',
        'AWAITING_CONFIRMATION',
        'CONFIRMED',
        'PREPARING',
        'READY',
        'ASSIGNED',
        'PICKED',
        'DELIVERED',
        'CANCELLED',
        'DISPUTED',
        'RESOLVED',
        'REFUNDED',
        'EXPIRED'
    ]

    ALLOWED_TRANSITIONS = {
        'PENDING_PAYMENT': ['CREATED', 'CANCELLED'],
        'PENDING_MANUAL_ASSIGNMENT': ['CONFIRMED', 'PREPARING', 'CANCELLED'],
        'CREATED': ['AWAITING_CONFIRMATION', 'CONFIRMED', 'CANCELLED'],
        'AWAITING_CONFIRMATION': ['CONFIRMED', 'CANCELLED'],
        'CONFIRMED': ['PREPARING', 'CANCELLED'],
        'PREPARING': ['READY', 'CANCELLED'],
        'READY': ['ASSIGNED', 'DELIVERED', 'CANCELLED'],
        'ASSIGNED': ['PICKED', 'CANCELLED'],
        'PICKED': ['DELIVERED', 'CANCELLED', 'DISPUTED'],
        'DELIVERED': ['DISPUTED'],
        'CANCELLED': ['REFUNDED', 'DISPUTED'],
        'DISPUTED': ['RESOLVED', 'REFUNDED', 'CANCELLED'],
        'RESOLVED': [],
        'REFUNDED': [],
        'EXPIRED': ['CANCELLED'],
    }

    @staticmethod
    def transition(current_state, next_state):
        if current_state not in OrderFSM.STATES or next_state not in OrderFSM.STATES:
            raise InvalidStateTransitionError(f"Invalid states provided: {current_state} -> {next_state}")

        if next_state not in OrderFSM.ALLOWED_TRANSITIONS.get(current_state, []):
            raise InvalidStateTransitionError(f"Cannot transition order from {current_state} to {next_state}")

        return next_state

    @staticmethod
    def ensure(current_state, next_state):
        """
        Idempotent variant of transition: already being in the target state is
        a no-op, anything else is validated strictly. Use this only where a
        double-submit is expected (driver/restaurant app buttons); do NOT use
        it to paper over genuine illegal transitions.
        """
        if current_state == next_state:
            return next_state
        return OrderFSM.transition(current_state, next_state)
