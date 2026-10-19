import logging

from domain.events.bus import EventBus
from domain.events.types import (
    PaymentCollected,
    PaymentDisbursed,
    PaymentInitiated,
    PaymentRefunded,
    PaymentReleaseRequested,
)
from domain.services.payment_services import ReleasePaymentService, record_failed_payout
from infrastructure.notifications.messages import (
    driver_earning_credited,
    refund_processed,
    vendor_funds_received,
)
from infrastructure.notifications.service import NotificationService

logger = logging.getLogger(__name__)


def on_payment_release_requested(event: PaymentReleaseRequested):
    logger.info(f"Releasing funds for payment {event.aggregate_id}")
    try:
        ReleasePaymentService.execute(event.aggregate_id)
        logger.info(f"Successfully released payment {event.aggregate_id}")
    except Exception as e:
        logger.error(f"Failed to release payment {event.aggregate_id}: {str(e)}")
        from apps.payments.models import Payment
        payment = Payment.objects.filter(id=event.aggregate_id).first()
        if payment is not None:
            record_failed_payout(payment, str(e))


def on_payment_collected(event: PaymentCollected):
    payable_type = event.payload.get('payable_type')
    payable_id = event.payload.get('payable_id')
    logger.info(f"Payment collected for {payable_type} {payable_id}")
    # Order confirmation is handled synchronously inside CompletePaymentService._handle_order_payment.
    # Nothing extra to do here — the handler is kept for future extensibility and logging.


def on_payment_initiated(event: PaymentInitiated):
    logger.info(f"Payment initiated for order {event.payload.get('order_id')}")


def on_payment_disbursed(event: PaymentDisbursed):
    order_id = event.payload.get('order_id')
    logger.info(f"Funds disbursed for order {order_id}")

    from apps.orders.models import Order
    from apps.payments.models import Payment

    try:
        payment = Payment.objects.get(id=event.aggregate_id)
        order = Order.objects.select_related('restaurant__owner', 'driver__user').get(id=order_id)

        if order.restaurant and order.restaurant.owner:
            v_title, v_body = vendor_funds_received(str(order.id)[:8])
            NotificationService.send_push(
                user_id=str(order.restaurant.owner.id),
                title=v_title, body=v_body,
                data={"order_id": str(order.id), "type": "payment_received"},
            )
        if order.driver and order.driver.user:
            d_title, d_body = driver_earning_credited(str(order.id)[:8])
            NotificationService.send_push(
                user_id=str(order.driver.user.id),
                title=d_title, body=d_body,
                data={"order_id": str(order.id), "type": "payout_received"},
            )
    except Payment.DoesNotExist:
        logger.error(f"Payment {event.aggregate_id} not found for disbursement")
    except Order.DoesNotExist:
        logger.error(f"Order {order_id} not found for disbursement notification")


def on_payment_refunded(event: PaymentRefunded):
    payable_id = event.payload.get('payable_id')
    logger.info(f"Payment refunded for {event.payload.get('type')} {payable_id}")

    payable_type = event.payload.get('type')
    from apps.orders.models import Order, PoolMember
    try:
        if payable_type == 'order':
            order = Order.objects.select_related('customer').get(id=payable_id)
            user_id = str(order.customer.id)
            short_id = str(order.id)[:8]
            data = {"order_id": str(order.id), "type": "refund_processed"}
        elif payable_type == 'pool_member':
            member = PoolMember.objects.select_related('user', 'pool').get(id=payable_id)
            user_id = str(member.user.id)
            short_id = str(member.pool_id)[:8]
            data = {"pool_id": str(member.pool_id), "type": "refund_processed"}
        else:
            return

        r_title, r_body = refund_processed(short_id)
        NotificationService.send_push(
            user_id=user_id,
            title=r_title, body=r_body,
            data=data,
        )
    except Order.DoesNotExist:
        logger.error(f"Order {payable_id} not found for refund notification")
    except PoolMember.DoesNotExist:
        logger.error(f"Pool member {payable_id} not found for refund notification")


def register_handlers():
    EventBus.subscribe(PaymentReleaseRequested, on_payment_release_requested)
    EventBus.subscribe(PaymentCollected, on_payment_collected)
    EventBus.subscribe(PaymentInitiated, on_payment_initiated)
    EventBus.subscribe(PaymentDisbursed, on_payment_disbursed)
    EventBus.subscribe(PaymentRefunded, on_payment_refunded)
