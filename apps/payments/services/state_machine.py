from ..models import Event

ALLOWED_TRANSITIONS = {
    'created':['awaiting_payment'],
    'awaiting_payment':['paid', 'cancelled'],
    'paid':['accepted'],
    'accepted':['preparing'],
    'preparing':['ready'],
    'ready':['driver_assigned'],
    'driver_assigned':['picked_up'],
    'picked_up':['delivered'],
    'delivered':['completed']
}

def transition_order(order, new_status):
    allowed = ALLOWED_TRANSITIONS.get(order.status, [])

    if new_status not in allowed:
        raise Exception('Invalid State transition')
    
    order.status = new_status
    order.save()

    Event.objects.create(
        event_type = f'order.{new_status}',
        entity_id = order.id
    )