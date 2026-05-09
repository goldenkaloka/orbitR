class InvalidStateTransitionError(Exception):
    pass

class OrderFSM:
    STATES = [
        'PENDING_PAYMENT',
        'CREATED',
        'CONFIRMED',
        'PREPARING',
        'READY',
        'OFFERED',
        'ASSIGNED',
        'PICKED',
        'DELIVERED',
        'CANCELLED',
        'DISPUTED',
        'REFUNDED'
    ]

    ALLOWED_TRANSITIONS = {
        'PENDING_PAYMENT': ['CREATED', 'CANCELLED'],
        'CREATED': ['CONFIRMED', 'CANCELLED'],
        'CONFIRMED': ['PREPARING', 'CANCELLED'],
        'PREPARING': ['READY', 'CANCELLED'],
        'READY': ['OFFERED', 'CANCELLED'],
        'OFFERED': ['ASSIGNED', 'READY', 'CANCELLED'],
        'ASSIGNED': ['PICKED', 'CANCELLED'],
        'PICKED': ['DELIVERED', 'CANCELLED'],
        'DELIVERED': ['DISPUTED'],
        'CANCELLED': ['REFUNDED'],
        'DISPUTED': ['REFUNDED'],
        'REFUNDED': []
    }

    @staticmethod
    def transition(current_state, next_state):
        if current_state not in OrderFSM.STATES or next_state not in OrderFSM.STATES:
            raise InvalidStateTransitionError(f"Invalid states provided: {current_state} -> {next_state}")
        
        if next_state not in OrderFSM.ALLOWED_TRANSITIONS.get(current_state, []):
            raise InvalidStateTransitionError(f"Cannot transition order from {current_state} to {next_state}")
        
        return next_state
