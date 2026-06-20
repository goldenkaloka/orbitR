import logging

from domain.events.bus import EventBus
from domain.events.types import (
    PaymentCollected,
    PaymentDisbursed,
    PaymentInitiated,
    PaymentRefunded,
    PaymentReleaseRequested,
)
from domain.services.payment_services import ReleasePaymentService
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


def on_payment_collected(event: PaymentCollected):
    payable_type = event.payload.get('payable_type')
    payable_id = event.payload.get('payable_id')
    logger.info(f"Payment collected for {payable_type} {payable_id}")

    if payable_type != 'order':
        return

    try:
        from apps.orders.models import Order
        from django.db import transaction
        from domain.services.order_services import ConfirmOrderService
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=payable_id)
            if order.status != 'CREATED':
                logger.info(f"Order {payable_id} already in {order.status} state. Skipping.")
                return
            order.is_paid = True
            order.save(update_fields=['is_paid', 'updated_at'])
            ConfirmOrderService.execute(payable_id, order.customer)
    except Exception as e:
        logger.error(f"Failed to confirm order after payment: {str(e)}")


def on_payment_initiated(event: PaymentInitiated):
    logger.info(f"Payment initiated for order {event.payload.get('order_id')}")


def on_payment_disbursed(event: PaymentDisbursed):
    order_id = event.payload.get('order_id')
    logger.info(f"Funds disbursed for order {order_id}")

    from decimal import Decimal

    from apps.orders.models import Order
    from apps.payments.models import Payment, Transaction
    from apps.users.models import User
    from django.db import transaction
    from domain.services.wallet_services import WalletDomainService

    try:
        payment = Payment.objects.get(id=event.aggregate_id)
        restaurant_owner_id = event.payload.get('restaurant_owner_id')
        restaurant_payout_str = event.payload.get('restaurant_payout', '0')
        driver_user_id = event.payload.get('driver_user_id')
        driver_payout_str = event.payload.get('driver_payout', '0')

        restaurant_payout = Decimal(restaurant_payout_str)
        driver_payout = Decimal(driver_payout_str)

        with transaction.atomic():
            if restaurant_owner_id and restaurant_payout > 0:
                restaurant_owner = User.objects.get(id=restaurant_owner_id)
                WalletDomainService.credit(restaurant_owner, restaurant_payout, f"Earnings from Order #{order_id}")
                Transaction.objects.create(
                    payment=payment, to_wallet=restaurant_owner,
                    amount=restaurant_payout, transaction_type='platform_to_restaurant',
                    status='COMPLETED',
                )

            if driver_user_id and driver_payout > 0:
                driver_user = User.objects.get(id=driver_user_id)
                WalletDomainService.credit(driver_user, driver_payout, f"Delivery for Order #{order_id}")
                Transaction.objects.create(
                    payment=payment, to_wallet=driver_user,
                    amount=driver_payout, transaction_type='platform_to_driver',
                    status='COMPLETED',
                )

        order = Order.objects.select_related('restaurant__owner', 'driver__user').get(id=order_id)
        v_title, v_body = vendor_funds_received(str(order.id)[:8])
        NotificationService.send_push(
            user_id=str(order.restaurant.owner.id),
            title=v_title, body=v_body,
            data={"order_id": str(order.id), "type": "payment_received"},
        )
        if order.driver:
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

    if event.payload.get('type') != 'order':
        return

    order_id = payable_id
    from apps.orders.models import Order
    try:
        order = Order.objects.select_related('customer').get(id=order_id)
        r_title, r_body = refund_processed(str(order.id)[:8])
        NotificationService.send_push(
            user_id=str(order.customer.id),
            title=r_title, body=r_body,
            data={"order_id": str(order.id), "type": "refund_processed"},
        )
    except Order.DoesNotExist:
        logger.error(f"Order {order_id} not found for refund notification")


def register_handlers():
    EventBus.subscribe(PaymentReleaseRequested, on_payment_release_requested)
    EventBus.subscribe(PaymentCollected, on_payment_collected)
    EventBus.subscribe(PaymentInitiated, on_payment_initiated)
    EventBus.subscribe(PaymentDisbursed, on_payment_disbursed)
    EventBus.subscribe(PaymentRefunded, on_payment_refunded)
