from domain.fsm.order_fsm import InvalidStateTransitionError

class PaymentFSM:
    STATES = [
        'INITIATED',
        'COLLECTED',
        'ON_HOLD',
        'DISBURSED',
        'REFUNDED',
        'FAILED'
    ]

    ALLOWED_TRANSITIONS = {
        'INITIATED': ['COLLECTED', 'FAILED'],
        'COLLECTED': ['ON_HOLD', 'DISBURSED', 'REFUNDED', 'FAILED'],
        'ON_HOLD': ['DISBURSED', 'REFUNDED', 'FAILED'],
        'DISBURSED': [],
        'FAILED': ['INITIATED'],  # Retry transitions back to initiated
    }

    @staticmethod
    def transition(current_state, next_state):
        if current_state not in PaymentFSM.STATES or next_state not in PaymentFSM.STATES:
            raise InvalidStateTransitionError(f"Invalid states provided: {current_state} -> {next_state}")
        
        if next_state not in PaymentFSM.ALLOWED_TRANSITIONS.get(current_state, []):
            raise InvalidStateTransitionError(f"Cannot transition payment from {current_state} to {next_state}")
        
        return next_state
