from apps.payments.models import Payment, Transaction, FailedPayout
from django.db import transaction
from decimal import Decimal
from django.utils import timezone
from apps.orders.models import Order
from domain.fsm.payment_fsm import PaymentFSM
from infrastructure.payment_gateways.airpay import AirpayGateway
from domain.events.bus import EventBus
from domain.events.types import PaymentCollected, PaymentInitiated, PaymentDisbursed, PaymentRefunded

class InitiatePaymentService:
    @staticmethod
    def execute(order_id, provider=None, phone=None):
        with transaction.atomic():
            order = Order.objects.get(id=order_id)
            
            # Create or get Payment record with locking
            payment, created = Payment.objects.select_for_update().get_or_create(
                order=order,
                defaults={'amount': order.total_amount, 'status': 'INITIATED'}
            )

            # Prevent double payment (FSM check)
            if payment.status in ['COLLECTED', 'RELEASE_READY', 'DISBURSED']:
                raise ValueError("This order has already been paid.")

            # Prevent double payment (Ledger check)
            if Transaction.objects.filter(payment__order=order, status='COMPLETED').exists():
                raise ValueError("A completed transaction already exists for this order.")

            # Use Airpay for all flows (Redirect based)
            gateway = AirpayGateway()

            response = gateway.collect(
                amount=payment.amount,
                phone=phone or order.customer.phone,
                order_reference=str(order.id),
                provider=provider
            )
            
            # Update payment with provider info if returned
            if response.get('success'):
                payment.provider_reference = response.get('transactionId') or response.get('provider_ref')
                payment.save()
                
                EventBus.publish(PaymentInitiated(
                    aggregate_id=payment.id,
                    payload={'order_id': str(order.id), 'provider': provider}
                ))
            
            return {
                'payment': payment,
                'gateway_response': response
            }


class CompletePaymentService:
    @staticmethod
    def execute(provider_reference, success=True, order_id=None, rrn=None):
        with transaction.atomic():
            payment = None
            if provider_reference:
                payment = Payment.objects.filter(provider_reference=provider_reference).first()
            
            if not payment and order_id:
                payment = Payment.objects.filter(order_id=order_id).first()
            
            if not payment:
                raise ValueError(f"No payment found for Ref: {provider_reference} or Order: {order_id}")
            
            if success:
                new_state = PaymentFSM.transition(payment.status, 'COLLECTED')
                payment.status = new_state
                payment.provider_reference = provider_reference
                payment.rrn = rrn
                payment.save()
                
                # Ledger Entry: Customer -> Platform (Held in Escrow)
                Transaction.objects.create(
                    payment=payment,
                    from_wallet=payment.order.customer,
                    to_wallet=None, # Platform incoming
                    amount=payment.amount,
                    transaction_type='customer_to_platform',
                    status='COMPLETED'
                )
                
                EventBus.publish(PaymentCollected(
                    aggregate_id=payment.id,
                    payload={'order_id': str(payment.order.id)}
                ))
            else:
                new_state = PaymentFSM.transition(payment.status, 'FAILED')
                payment.status = new_state
                payment.save()
                
            return payment


class ReleasePaymentService:
    """
    Escrow Release: Distributes funds to Restaurant and Driver using Airpay Split Settlement.
    """
    @staticmethod
    def execute(payment_id):
        with transaction.atomic():
            payment = Payment.objects.select_related(
                'order', 
                'order__restaurant__owner', 
                'order__driver__user'
            ).get(id=payment_id)
            
            order = payment.order
            gateway = AirpayGateway()
            
            # Define Splits
            restaurant_payout = float(order.items_subtotal * Decimal('0.90'))
            driver_payout = float(order.delivery_fee) if order.driver else 0.0
            platform_cut = float(payment.amount) - restaurant_payout - driver_payout

            splits = [
                {'identifier': 'PLATFORM_ESCROW', 'value': platform_cut},
                {'identifier': order.restaurant.owner.phone, 'value': restaurant_payout},
            ]
            
            if order.driver:
                splits.append({'identifier': order.driver.user.phone, 'value': driver_payout})

            # Trigger Airpay Split Settlement
            response = gateway.split_settlement(
                airpay_id=payment.provider_reference,
                rrn=payment.rrn,
                splits=splits
            )
            
            if response and response.get('success'):
                payment.status = PaymentFSM.transition(payment.status, 'DISBURSED')
                payment.save()
                
                # Create detailed transactions
                Transaction.objects.create(
                    payment=payment,
                    from_wallet=None,
                    to_wallet=order.restaurant.owner,
                    amount=restaurant_payout,
                    transaction_type='platform_to_restaurant',
                    status='COMPLETED'
                )
                if order.driver:
                    Transaction.objects.create(
                        payment=payment,
                        from_wallet=None,
                        to_wallet=order.driver.user,
                        amount=driver_payout,
                        transaction_type='platform_to_driver',
                        status='COMPLETED'
                    )
                
                EventBus.publish(PaymentDisbursed(
                    aggregate_id=payment.id,
                    payload={'order_id': str(order.id)}
                ))
            else:
                # Log failure
                FailedPayout.objects.create(
                    payment=payment,
                    to_wallet=order.restaurant.owner,
                    amount=restaurant_payout,
                    transaction_type='split_settlement_failure',
                    reason=response.get('error', 'Unknown Error'),
                    retry_count=1
                )
            
            return payment


class RefundPaymentService:
    """
    Escrow Refund: Returns funds to the customer.
    """
    @staticmethod
    def execute(order_id):
        with transaction.atomic():
            payment = Payment.objects.filter(order_id=order_id, status='COLLECTED').first()
            if not payment:
                raise ValueError("No refundable payment found for this order")
                
            gateway = AirpayGateway()
            
            response = gateway.refund(
                airpay_id=payment.provider_reference,
                amount=payment.amount
            )
            
            if response and response.get('success'):
                payment.status = PaymentFSM.transition(payment.status, 'REFUNDED')
                payment.refund_id = response.get('refund_id')
                payment.refunded_at = timezone.now()
                payment.save()
                
                # Transaction reversal entry
                Transaction.objects.create(
                    payment=payment,
                    from_wallet=None, # Platform outgoing
                    to_wallet=payment.order.customer,
                    amount=payment.amount,
                    transaction_type='platform_to_customer_refund',
                    status='COMPLETED'
                )
                
                EventBus.publish(PaymentRefunded(
                    aggregate_id=payment.id,
                    payload={'order_id': str(payment.order.id)}
                ))
                return True
            
            return False


