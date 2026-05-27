class InvalidStateTransitionError(Exception):
    pass

class OrderFSM:
    STATES = [
        'PENDING_PAYMENT',
        'CREATED',
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
        'PENDING_PAYMENT': ['CREATED', 'CANCELLED', 'PENDING_PAYMENT'],
        'CREATED': ['CONFIRMED', 'CANCELLED', 'CREATED'],
        'CONFIRMED': ['PREPARING', 'CANCELLED', 'CONFIRMED'],
        'PREPARING': ['READY', 'CANCELLED', 'PREPARING'],
        'READY': ['ASSIGNED', 'CANCELLED', 'READY'],
        'ASSIGNED': ['PICKED', 'CANCELLED', 'ASSIGNED'],
        'PICKED': ['DELIVERED', 'CANCELLED', 'DISPUTED', 'PICKED'],
        'DELIVERED': ['DISPUTED', 'DELIVERED'],
        'CANCELLED': ['REFUNDED', 'DISPUTED', 'CANCELLED'],
        'DISPUTED': ['RESOLVED', 'REFUNDED', 'CANCELLED', 'DISPUTED'],
        'RESOLVED': ['RESOLVED'],
        'REFUNDED': ['REFUNDED'],
        'EXPIRED': ['CANCELLED', 'EXPIRED'],
    }

    @staticmethod
    def transition(current_state, next_state):
        if current_state not in OrderFSM.STATES or next_state not in OrderFSM.STATES:
            raise InvalidStateTransitionError(f"Invalid states provided: {current_state} -> {next_state}")

        if next_state not in OrderFSM.ALLOWED_TRANSITIONS.get(current_state, []):
            raise InvalidStateTransitionError(f"Cannot transition order from {current_state} to {next_state}")

        return next_state
