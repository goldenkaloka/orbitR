from domain.fsm.order_fsm import InvalidStateTransitionError

class DriverFSM:
    STATES = [
        'ONLINE',
        'OFFLINE',
        'AVAILABLE',
        'DELIVERING'
    ]

    ALLOWED_TRANSITIONS = {
        'ONLINE': ['AVAILABLE', 'DELIVERING', 'OFFLINE', 'ONLINE'],
        'OFFLINE': ['AVAILABLE', 'OFFLINE'],
        'AVAILABLE': ['OFFLINE', 'DELIVERING', 'AVAILABLE'],
        'DELIVERING': ['AVAILABLE', 'DELIVERING']
    }

    @staticmethod
    def transition(current_state, next_state):
        if current_state not in DriverFSM.STATES or next_state not in DriverFSM.STATES:
            raise InvalidStateTransitionError(f"Invalid states provided: {current_state} -> {next_state}")

        if next_state not in DriverFSM.ALLOWED_TRANSITIONS.get(current_state, []):
            raise InvalidStateTransitionError(f"Cannot transition driver from {current_state} to {next_state}")

        return next_state
