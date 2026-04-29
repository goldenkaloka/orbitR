class InvalidStateTransitionError(Exception):
    pass

class OrderFSM:
    STATES = [
        'CREATED',
        'CONFIRMED',
        'PREPARING',
        'READY',
        'ASSIGNED',
        'PICKED',
        'DELIVERED',
        'CANCELLED',
        'DISPUTED',
        'REFUNDED'
    ]

    ALLOWED_TRANSITIONS = {
        'CREATED': ['CONFIRMED', 'CANCELLED'],
        'CONFIRMED': ['PREPARING', 'CANCELLED'],
        'PREPARING': ['READY', 'CANCELLED'],
        'READY': ['ASSIGNED', 'CANCELLED'],
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
