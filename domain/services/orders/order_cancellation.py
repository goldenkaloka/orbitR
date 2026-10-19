import logging
from decimal import Decimal

from apps.delivery.models import DeliveryAssignment
from apps.orders.models import Order, OrderItem
from django.db import models, transaction
from django.utils import timezone
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

DEFAULT_CANCELLATION_CATEGORY_BY_ROLE = {
    'customer': 'customer_changed_mind',
    'vendor': 'vendor_rejected',
    'vendor_staff': 'vendor_rejected',
    'driver': 'driver_cancelled',
    'admin': 'admin_cancelled',
}

PUBLIC_RESTAURANT_FAULT_CATEGORIES = {
    'vendor_rejected',
    'restaurant_delay',
}

def _is_vendor_or_staff(actor, order):
    if actor.role == 'vendor' and order.restaurant.owner == actor:
        return True
    from apps.restaurants.models import SectionStaff
    return SectionStaff.objects.filter(
        user=actor, section__restaurant=order.restaurant
    ).exists()


def _pool_member_id_for_order(order):
    if not getattr(order, 'is_pool_order', False):
        return None
    breakdown = getattr(order, 'group_breakdown', None) or {}
    member_id = breakdown.get('member_id') if isinstance(breakdown, dict) else None
    if member_id:
        return member_id
    if getattr(order, 'pool_id', None):
        member = order.pool.members.filter(user_id=order.customer_id).only('id').first()
        return member.id if member else None
    return None


def _cancel_actor_type(actor, order=None):
    if getattr(actor, 'username', None) == 'system_dispatcher':
        return 'SYSTEM'
    if getattr(actor, 'role', None) == 'customer':
        return 'CUSTOMER'
    if getattr(actor, 'role', None) == 'driver':
        return 'DRIVER'
    if getattr(actor, 'role', None) == 'admin' or getattr(actor, 'is_staff', False):
        return 'ADMIN'
    if order is not None and _is_vendor_or_staff(actor, order):
        return 'VENDOR'
    if getattr(actor, 'role', None) in VENDOR_LIKE_ROLES:
        return 'VENDOR'
    return 'SYSTEM'


def _fault_for_actor(actor_type):
    if actor_type in ('CUSTOMER', 'VENDOR', 'DRIVER'):
        return actor_type
    if actor_type == 'ADMIN':
        return 'PLATFORM'
    return 'SYSTEM'


def _normalize_cancellation_category(category, actor_type):
    if category:
        return str(category)
    role = actor_type.lower()
    return DEFAULT_CANCELLATION_CATEGORY_BY_ROLE.get(role, 'system_timeout' if actor_type == 'SYSTEM' else 'other')


def _apply_cancellation_metadata(order, actor, reason, category=None, fault=None, affects_restaurant_reputation=None):
    actor_type = _cancel_actor_type(actor, order)
    normalized_category = _normalize_cancellation_category(category, actor_type)
    normalized_fault = fault or _fault_for_actor(actor_type)
    if affects_restaurant_reputation is None:
        affects_restaurant_reputation = (
            normalized_fault == 'VENDOR'
            and normalized_category in PUBLIC_RESTAURANT_FAULT_CATEGORIES
        )

    order.cancelled_at = timezone.now()
    order.cancelled_by = actor_type
    order.cancellation_fault = normalized_fault
    order.cancellation_category = normalized_category
    order.cancellation_reason = reason
    order.affects_restaurant_reputation = affects_restaurant_reputation
    return {
        'cancelled_by': actor_type,
        'cancellation_fault': normalized_fault,
        'cancellation_category': normalized_category,
        'affects_restaurant_reputation': affects_restaurant_reputation,
    }


class CancelOrderService:
    @staticmethod
    def execute(order_id, actor, reason='No reason provided', category=None, fault=None, affects_restaurant_reputation=None):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)

            # Idempotent double-tap: cancelling an already-cancelled order is a
            # no-op (would otherwise re-run metadata application and refunds).
            if order.status == 'CANCELLED':
                return order

            cancel_status = None
            if actor.role == 'customer' and order.customer == actor:
                if order.status not in ['PENDING_PAYMENT', 'CREATED', 'AWAITING_CONFIRMATION', 'CONFIRMED']:
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
            metadata = _apply_cancellation_metadata(
                order,
                actor,
                reason,
                category=category,
                fault=fault,
                affects_restaurant_reputation=affects_restaurant_reputation,
            )

            if hasattr(order, 'payment') and order.payment:
                from domain.fsm.payment_fsm import PaymentFSM
                if order.payment.status == 'INITIATED':
                    order.payment.status = PaymentFSM.transition(order.payment.status, 'FAILED')
                    order.payment.save(update_fields=['status'])

            if order.is_paid and getattr(order, 'payment', None):
                fee_pct = Decimal('0')
                customer_caused_paid_cancel = (
                    actor.role == 'customer'
                    and order.customer_id == actor.id
                    and cancel_status
                    and cancel_status != 'PENDING_PAYMENT'
                )
                if customer_caused_paid_cancel:
                    from apps.reliability.services import get_cancel_fee_percent, get_reliability
                    record = get_reliability(actor, actor.role)
                    fee_pct = get_cancel_fee_percent(record.score)
                RefundPaymentService.execute('order', order.id, cancel_fee_pct=fee_pct)
            elif order.is_paid and getattr(order, 'is_pool_order', False):
                member_id = _pool_member_id_for_order(order)
                if member_id:
                    RefundPaymentService.execute('pool_member', member_id)
                    order.pool.members.filter(id=member_id).update(status='REFUNDED')

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
                    **metadata,
                }
            )))
            return order


class CancelOrderItemService:
    @staticmethod
    def execute(order_id, item_id, actor, reason='Item cancelled by restaurant', category='item_unavailable'):
        from apps.restaurants.models import SectionStaff

        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            from domain.services.permissions import check_restaurant_permission
            if not order.restaurant or not check_restaurant_permission(actor, order.restaurant):
                raise PermissionError("Only restaurant staff can cancel items.")

            if order.status not in ('CONFIRMED', 'PREPARING'):
                raise PermissionError("Can only cancel items while order is being prepared.")

            item = (
                OrderItem.objects.select_for_update()
                .filter(id=item_id, order_id=order_id)
                .first()
            )
            if item is None:
                matching_items = list(
                    OrderItem.objects.select_for_update().filter(
                        food_id=item_id,
                        order_id=order_id,
                        cancelled_quantity__lt=models.F('quantity'),
                    )
                )
                if len(matching_items) != 1:
                    raise ValueError("Item not found or ambiguous. Send the order item id.")
                item = matching_items[0]
            if item.active_quantity <= 0:
                raise ValueError("Item already fully cancelled.")

            cancelled_qty = item.active_quantity
            item.cancelled_quantity = item.quantity
            item.save(update_fields=['cancelled_quantity'])

            all_cancelled = all(
                i.active_quantity <= 0 for i in OrderItem.objects.filter(order_id=order_id)
            )
            if all_cancelled:
                CancelOrderService.execute(
                    order_id,
                    actor,
                    reason='All items cancelled by restaurant',
                    category=category,
                    fault='VENDOR',
                    affects_restaurant_reputation=False,
                )
            else:
                item_price = item.price / item.quantity if item.quantity > 0 else Decimal('0')
                refund_amount = item_price * cancelled_qty
                if order.is_paid and refund_amount > 0:
                    if getattr(order, 'payment', None):
                        RefundPaymentService.execute('order', order.id, override_amount=refund_amount)
                    elif getattr(order, 'is_pool_order', False):
                        member_id = _pool_member_id_for_order(order)
                        if member_id:
                            RefundPaymentService.execute('pool_member', member_id, override_amount=refund_amount)
                            order.pool.members.filter(id=member_id).update(
                                items_subtotal=models.F('items_subtotal') - refund_amount
                            )

                all_items = list(OrderItem.objects.filter(order_id=order_id))
                remaining_subtotal = sum(
                    (i.price / i.quantity) * i.active_quantity
                    for i in all_items if i.active_quantity > 0
                )
                order.items_subtotal = remaining_subtotal
                order.total_amount = remaining_subtotal + order.delivery_fee + order.service_fee
                order.save(update_fields=['items_subtotal', 'total_amount'])

            return item
