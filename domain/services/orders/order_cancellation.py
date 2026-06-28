import logging

from apps.delivery.models import DeliveryAssignment
from apps.orders.models import Order
from django.db import transaction
from domain.events.bus import EventBus
from domain.events.types import OrderCancelled
from domain.fsm.order_fsm import OrderFSM
from domain.services.driver_service import release_driver
from domain.services.payment_services import RefundPaymentService

logger = logging.getLogger(__name__)

STRIKE_REASON_MAP = {
    'customer': {
        'CREATED': 'cancel_created',
        'CONFIRMED': 'cancel_confirmed',
    },
    'vendor': {
        'CONFIRMED': 'cancel_confirmed',
        'PREPARING': 'cancel_preparing',
    },
    'driver': {
        'ASSIGNED': 'cancel_assigned',
        'PICKED': 'cancel_picked',
    },
}


class CancelOrderService:
    @staticmethod
    def execute(order_id, actor, reason='No reason provided'):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)

            cancel_status = None
            if actor.role == 'customer' and order.customer == actor:
                if order.status not in ['PENDING_PAYMENT', 'CREATED', 'CONFIRMED']:
                    raise PermissionError("Cannot cancel after preparation started.")
                cancel_status = order.status
            elif actor.role == 'vendor':
                if order.restaurant.owner != actor:
                    raise PermissionError("No permission to cancel this order.")
                cancel_status = order.status
            elif actor.role == 'driver':
                if order.driver != actor.driver_profile:
                    raise PermissionError("Not the assigned driver.")
                if order.status not in ['ASSIGNED', 'PICKED']:
                    raise PermissionError("Cannot cancel order in current status.")
                cancel_status = order.status
            elif actor.role != 'admin':
                raise PermissionError("No permission to cancel.")

            new_state = OrderFSM.transition(order.status, 'CANCELLED')
            order.status = new_state

            if hasattr(order, 'payment') and order.payment:
                from domain.fsm.payment_fsm import PaymentFSM
                if order.payment.status == 'INITIATED':
                    order.payment.status = PaymentFSM.transition(order.payment.status, 'FAILED')
                    order.payment.save(update_fields=['status'])

            if order.is_paid and getattr(order, 'payment', None):
                from decimal import Decimal
                fee_pct = Decimal('0')
                if cancel_status and cancel_status != 'PENDING_PAYMENT':
                    from apps.reliability.services import get_cancel_fee_percent, get_reliability
                    record = get_reliability(actor, actor.role)
                    fee_pct = get_cancel_fee_percent(record.score)
                RefundPaymentService.execute('order', order.id, cancel_fee_pct=fee_pct)

            if order.driver:
                release_driver(order.driver, 1)
                DeliveryAssignment.objects.filter(order=order).delete()

            order.save()

            if cancel_status:
                reason_map = STRIKE_REASON_MAP.get(actor.role, {})
                strike_reason = reason_map.get(cancel_status)
                if strike_reason:
                    from apps.reliability.services import add_strike
                    add_strike(actor, actor.role, strike_reason, order=order)

            transaction.on_commit(lambda: EventBus.publish(OrderCancelled(
                aggregate_id=order.id, payload={
                    'actor_id': str(actor.id),
                    'reason': reason,
                }
            )))
            return order
