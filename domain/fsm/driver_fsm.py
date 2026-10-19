from domain.fsm.order_fsm import InvalidStateTransitionError


class DriverFSM:
    STATES = [
        'ONLINE',
        'OFFLINE',
        'AVAILABLE',
        'DELIVERING'
    ]

    ALLOWED_TRANSITIONS = {
        'ONLINE': ['AVAILABLE', 'DELIVERING', 'OFFLINE'],
        'OFFLINE': ['AVAILABLE'],
        'AVAILABLE': ['OFFLINE', 'DELIVERING'],
        'DELIVERING': ['AVAILABLE']
    }

    @staticmethod
    def transition(current_state, next_state):
        if current_state not in DriverFSM.STATES or next_state not in DriverFSM.STATES:
            raise InvalidStateTransitionError(f"Invalid states provided: {current_state} -> {next_state}")

        if next_state not in DriverFSM.ALLOWED_TRANSITIONS.get(current_state, []):
            raise InvalidStateTransitionError(f"Cannot transition driver from {current_state} to {next_state}")

        return next_state

    @staticmethod
    def ensure(current_state, next_state):
        """
        Idempotent variant of transition: already being in the target state is
        a no-op (drivers double-tap the online/offline toggle), anything else
        is validated strictly.
        """
        if current_state == next_state:
            return next_state
        return DriverFSM.transition(current_state, next_state)
