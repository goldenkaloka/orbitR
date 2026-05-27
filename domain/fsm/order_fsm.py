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
        'RESOLVED',
        'REFUNDED',
        'PENDING_MANUAL_ASSIGNMENT',
        'DISPATCH_FAILED',
        'EXPIRED'
    ]

    ALLOWED_TRANSITIONS = {
        'PENDING_PAYMENT': ['CREATED', 'CANCELLED', 'PENDING_PAYMENT'],
        'CREATED': ['CONFIRMED', 'CANCELLED', 'CREATED'],
        'CONFIRMED': ['PREPARING', 'CANCELLED', 'CONFIRMED'],
        'PREPARING': ['READY', 'CANCELLED', 'PREPARING'],
        'READY': ['OFFERED', 'ASSIGNED', 'CANCELLED', 'READY', 'PENDING_MANUAL_ASSIGNMENT'],
        'OFFERED': ['ASSIGNED', 'READY', 'CANCELLED', 'OFFERED', 'PENDING_MANUAL_ASSIGNMENT', 'DISPATCH_FAILED'],
        'ASSIGNED': ['PICKED', 'CANCELLED', 'ASSIGNED'],
        'PICKED': ['DELIVERED', 'CANCELLED', 'PICKED'],
        'DELIVERED': ['DISPUTED', 'DELIVERED'],
        'PICKED': ['DELIVERED', 'CANCELLED', 'DISPUTED', 'PICKED'],
        'CANCELLED': ['REFUNDED', 'DISPUTED', 'CANCELLED'],
        'DISPUTED': ['RESOLVED', 'REFUNDED', 'CANCELLED', 'DISPUTED'],
        'RESOLVED': ['RESOLVED'],
        'REFUNDED': ['REFUNDED'],
        'PENDING_MANUAL_ASSIGNMENT': ['ASSIGNED', 'CANCELLED', 'PENDING_MANUAL_ASSIGNMENT'],
        'DISPATCH_FAILED': ['PENDING_MANUAL_ASSIGNMENT', 'CANCELLED', 'OFFERED', 'DISPATCH_FAILED'],
        'EXPIRED': ['CANCELLED', 'EXPIRED']
    }

    @staticmethod
    def transition(current_state, next_state):
        if current_state not in OrderFSM.STATES or next_state not in OrderFSM.STATES:
            raise InvalidStateTransitionError(f"Invalid states provided: {current_state} -> {next_state}")
        
        if next_state not in OrderFSM.ALLOWED_TRANSITIONS.get(current_state, []):
            raise InvalidStateTransitionError(f"Cannot transition order from {current_state} to {next_state}")
        
        return next_state
