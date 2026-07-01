import logging
from decimal import Decimal

from apps.delivery.models import DeliveryAssignment
from apps.orders.models import Order, OrderItem
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

VENDOR_LIKE_ROLES = {'vendor', 'vendor_staff'}

def _is_vendor_or_staff(actor, order):
    if actor.role == 'vendor' and order.restaurant.owner == actor:
        return True
    from apps.restaurants.models import SectionStaff
    return SectionStaff.objects.filter(
        user=actor, section__restaurant=order.restaurant
    ).exists()


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
            elif _is_vendor_or_staff(actor, order):
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
                role_key = 'vendor' if _is_vendor_or_staff(actor, order) and actor.role != 'vendor' else actor.role
                reason_map = STRIKE_REASON_MAP.get(role_key, {})
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


class CancelOrderItemService:
    @staticmethod
    def execute(order_id, item_id, actor, reason='Item cancelled by restaurant'):
        from apps.restaurants.models import SectionStaff

        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if not _is_vendor_or_staff(actor, order):
                raise PermissionError("Only restaurant staff can cancel items.")

            if order.status not in ('CONFIRMED', 'PREPARING'):
                raise PermissionError("Can only cancel items while order is being prepared.")

            item = OrderItem.objects.select_for_update().get(id=item_id, order_id=order_id)
            if item.active_quantity <= 0:
                raise ValueError("Item already fully cancelled.")

            cancelled_qty = item.active_quantity
            item.cancelled_quantity = item.quantity
            item.save(update_fields=['cancelled_quantity'])

            all_cancelled = all(
                i.active_quantity <= 0 for i in OrderItem.objects.filter(order_id=order_id)
            )
            if all_cancelled:
                CancelOrderService.execute(order_id, actor, reason='All items cancelled by restaurant')
            else:
                item_price = item.price / item.quantity if item.quantity > 0 else Decimal('0')
                refund_amount = item_price * cancelled_qty
                if order.is_paid and refund_amount > 0:
                    from domain.fsm.payment_fsm import PaymentFSM
                    if getattr(order, 'payment', None):
                        RefundPaymentService.execute('order', order.id, override_amount=refund_amount)

                all_items = list(OrderItem.objects.filter(order_id=order_id))
                remaining_subtotal = sum(
                    (i.price / i.quantity) * i.active_quantity
                    for i in all_items if i.active_quantity > 0
                )
                order.items_subtotal = remaining_subtotal
                order.total_amount = remaining_subtotal + order.delivery_fee + order.service_fee
                order.save(update_fields=['items_subtotal', 'total_amount'])

            return item
