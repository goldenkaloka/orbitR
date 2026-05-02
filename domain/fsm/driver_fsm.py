from domain.fsm.order_fsm import InvalidStateTransitionError

class DriverFSM:
    STATES = [
        'OFFLINE',
        'AVAILABLE',
        'ASSIGNED',
        'DELIVERING'
    ]

    ALLOWED_TRANSITIONS = {
        'OFFLINE': ['AVAILABLE'],
        'AVAILABLE': ['OFFLINE', 'ASSIGNED'],
        'ASSIGNED': ['AVAILABLE', 'DELIVERING'],
        'DELIVERING': ['AVAILABLE']
    }

    @staticmethod
    def transition(current_state, next_state):
        if current_state not in DriverFSM.STATES or next_state not in DriverFSM.STATES:
            raise InvalidStateTransitionError(f"Invalid states provided: {current_state} -> {next_state}")
        
        if next_state not in DriverFSM.ALLOWED_TRANSITIONS.get(current_state, []):
            raise InvalidStateTransitionError(f"Cannot transition driver from {current_state} to {next_state}")
        
        return next_state
