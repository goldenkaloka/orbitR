"""Concierge / platform-managed restaurant order handling.

OrbitR lists the restaurant's menu, but the restaurant itself is a third-party
business. Ops confirms availability and the final bill, then OrbitR pays the
restaurant from escrow. The driver only collects the food.
"""

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.orders.models import Order
from domain.events.bus import EventBus
from domain.events.types import OrderConfirmed
from domain.fsm.order_fsm import OrderFSM

logger = logging.getLogger(__name__)


def is_concierge_restaurant(restaurant):
    return bool(
        restaurant
        and (
            restaurant.source_type == 'PLATFORM_MANAGED'
            or restaurant.settlement_mode == 'PLATFORM_PREPAID'
        )
    )


class ConciergeOrderService:
    @staticmethod
    def confirm(order_id, actor, confirmed_total=None, note=''):
        """Ops has called the restaurant and confirmed the order."""
        with transaction.atomic():
            order = Order.objects.select_for_update().select_related(
                'restaurant',
            ).get(id=order_id)

            if actor.role != 'admin':
                raise PermissionError("Only OrbitR ops can confirm a concierge order.")

            if not is_concierge_restaurant(order.restaurant):
                raise ValueError("This order is not a platform-managed/concierge order.")

            if order.confirmation_status == Order.CONFIRMATION_CONFIRMED:
                return order

            if order.status not in ('AWAITING_CONFIRMATION', 'CREATED'):
                raise ValueError(
                    f"Cannot confirm an order in status {order.status}."
                )

            if not order.is_paid:
                raise ValueError("Cannot confirm an unpaid order.")

            price_diff = Decimal('0')
            if confirmed_total is not None:
                new_total = Decimal(str(confirmed_total)).quantize(
                    Decimal('0.01')
                )
                if new_total < order.total_amount:
                    price_diff = order.total_amount - new_total
                order.confirmed_total = new_total
            elif order.confirmed_total is None:
                order.confirmed_total = order.total_amount

            if order.status != 'CONFIRMED':
                order.status = OrderFSM.transition(order.status, 'CONFIRMED')

            order.confirmation_status = Order.CONFIRMATION_CONFIRMED
            order.confirmed_at = timezone.now()
            if note:
                order.notes = (
                    f"{order.notes}\n[concierge] {note}" if order.notes else f"[concierge] {note}"
                )
            order.save()

            if price_diff > 0:
                from domain.services.payment_services import RefundPaymentService
                try:
                    RefundPaymentService.execute(
                        payable_type='order',
                        payable_id=str(order.id),
                        override_amount=price_diff,
                    )
                except Exception as exc:
                    logger.error(
                        f"Failed partial refund for order {order.id}: {exc}"
                    )

            def _on_commit():
                EventBus.publish(
                    OrderConfirmed(
                        aggregate_id=order.id,
                        payload={'actor_id': str(actor.id), 'concierge': True},
                    )
                )
                if price_diff > 0:
                    try:
                        from infrastructure.notifications.service import NotificationService
                        NotificationService.send_push(
                            user_id=str(order.customer_id),
                            title="Order Adjusted & Partial Refund",
                            body=(
                                f"The bill for #{str(order.id)[:8]} was confirmed at "
                                f"TSh {order.confirmed_total:,.0f}. TSh {price_diff:,.0f} has been refunded to you."
                            ),
                            data={"order_id": str(order.id), "type": "concierge_price_adjustment"},
                        )
                    except Exception as ne:
                        logger.warning(f"Could not notify customer of adjustment: {ne}")

            transaction.on_commit(_on_commit)
            return order

    @staticmethod
    def mark_unavailable(order_id, actor, reason='Restaurant could not fulfil the order.'):
        """Ops could not source the food; cancel and refund the customer."""
        from domain.services.orders.order_cancellation import CancelOrderService

        if actor.role != 'admin':
            raise PermissionError("Only OrbitR ops can mark a concierge order unavailable.")

        with transaction.atomic():
            order = Order.objects.select_for_update().select_related(
                'restaurant',
            ).get(id=order_id)
            if order.confirmation_status == Order.CONFIRMATION_UNAVAILABLE:
                return order
            order.confirmation_status = Order.CONFIRMATION_UNAVAILABLE
            order.save(update_fields=['confirmation_status', 'updated_at'])

        order = CancelOrderService.execute(
            order_id,
            actor,
            reason=reason,
            category='item_unavailable',
            fault='PLATFORM',
        )
        return order

    @staticmethod
    def pay_restaurant(order_id, actor, account_number=None, network=None):
        """OrbitR prepays the restaurant from escrow, before driver pickup."""
        from apps.payments.models import PickupDisbursement
        from domain.services.orders.pickup_payment import (
            _call_gateway,
            restaurant_net_share,
        )
        from domain.services.payment_services import (
            debit_escrow_external,
            restore_escrow_external,
        )

        with transaction.atomic():
            order = Order.objects.select_for_update().select_related(
                'restaurant',
            ).get(id=order_id)

            if actor.role != 'admin':
                raise PermissionError("Only OrbitR ops can prepay a restaurant.")

            restaurant = order.restaurant
            if not is_concierge_restaurant(restaurant):
                raise ValueError("This is not a platform-managed restaurant.")
            if restaurant.settlement_mode != restaurant.SETTLEMENT_PLATFORM_PREPAID:
                raise ValueError(
                    "This restaurant is not configured for OrbitR prepayment."
                )
            if order.confirmation_status != Order.CONFIRMATION_CONFIRMED:
                raise ValueError("Confirm the restaurant order before paying it.")

            existing = PickupDisbursement.objects.filter(order=order).first()
            if existing:
                if existing.status == PickupDisbursement.STATUS_SUCCESS:
                    return existing
                raise ValueError(
                    "A restaurant payment for this order is already pending or failed."
                )

            account = (
                str(account_number or restaurant.payout_phone or '').strip()
            )
            if not account:
                raise ValueError(
                    "Add the restaurant payout number before paying."
                )
            net = restaurant_net_share(order)
            if net <= 0:
                raise ValueError("Nothing to pay the restaurant for this order.")

            try:
                with transaction.atomic():
                    disbursement = PickupDisbursement.objects.create(
                        order=order,
                        batch=order.batch,
                        driver=None,
                        restaurant=restaurant,
                        amount=net,
                        account_number=account[:32],
                        network=str(network or restaurant.payout_network or '')[:32],
                        status=PickupDisbursement.STATUS_PENDING,
                    )
            except IntegrityError:
                return PickupDisbursement.objects.filter(order=order).first()

            debit_escrow_external(
                net,
                description=f"Concierge restaurant payout {str(order.id)[:8]}",
            )

        result = _call_gateway(net, account, order)

        with transaction.atomic():
            disbursement = PickupDisbursement.objects.select_for_update().get(
                id=disbursement.id
            )
            order = Order.objects.select_for_update().get(id=order.id)
            if result.get('success'):
                disbursement.status = PickupDisbursement.STATUS_SUCCESS
                disbursement.gateway_reference = str(
                    result.get('provider_reference') or ''
                )[:255]
                disbursement.settled_at = timezone.now()
                # Platform-managed restaurants settle through this path, so the
                # fee must be recorded here too — otherwise the main flow looks
                # fee-free and release books a cut the platform never received.
                from domain.services.gateway_fees import resolve_payout_fee
                disbursement.gateway_fee, disbursement.gateway_fee_source = \
                    resolve_payout_fee(result, net)
                disbursement.save()
                order.pickup_paid_amount = net
                order.restaurant_payment_status = Order.RESTAURANT_PAID
                order.restaurant_paid_amount = net
                order.restaurant_payment_reference = disbursement.gateway_reference
                order.restaurant_paid_at = timezone.now()
                order.restaurant_paid_by = actor
                order.save()
            else:
                disbursement.status = PickupDisbursement.STATUS_FAILED
                disbursement.failure_reason = str(
                    result.get('error') or 'Unknown gateway error'
                )[:2000]
                disbursement.save()
                restore_escrow_external(
                    net,
                    description=f"Reversal for failed concierge payout {str(order.id)[:8]}",
                )

        return disbursement
