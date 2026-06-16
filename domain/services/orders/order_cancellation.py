import logging
from django.db import transaction
from apps.orders.models import Order
from apps.delivery.models import DeliveryAssignment
from domain.fsm.order_fsm import OrderFSM
from domain.services.driver_service import release_driver
from domain.events.bus import EventBus
from domain.events.types import OrderCancelled
from domain.services.payment_services import RefundPaymentService

logger = logging.getLogger(__name__)


class CancelOrderService:
    @staticmethod
    def execute(order_id, actor, reason='No reason provided'):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)

            # Role-based validation
            if actor.role == 'customer' and order.customer == actor:
                if order.status not in ['CREATED', 'CONFIRMED']:
                    raise PermissionError("Cannot cancel after preparation started.")
            elif actor.role == 'vendor':
                if order.restaurant.owner != actor:
                    raise PermissionError("No permission to cancel this order.")
            elif actor.role == 'driver':
                if order.driver != actor.driver_profile:
                    raise PermissionError("Not the assigned driver.")
                if order.status not in ['ASSIGNED', 'PICKED']:
                    raise PermissionError("Cannot cancel order in current status.")
            elif actor.role != 'admin':
                raise PermissionError("No permission to cancel.")

            new_state = OrderFSM.transition(order.status, 'CANCELLED')
            order.status = new_state

            if order.is_paid:
                RefundPaymentService.execute('order', order.id)

            if order.driver:
                release_driver(order.driver, 1)
                DeliveryAssignment.objects.filter(order=order).delete()

            order.save()

            transaction.on_commit(lambda: EventBus.publish(OrderCancelled(
                aggregate_id=order.id, payload={
                    'actor_id': str(actor.id),
                    'reason': reason,
                }
            )))
            return order
