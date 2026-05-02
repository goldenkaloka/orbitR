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
                    payload={
                        'order_id': str(order.id),
                        'amount': float(payment.amount),
                        'gateway': 'Airpay'
                    }
                ))
            
            return {
                'payment': payment,
                'gateway_response': response
            }


class CompletePaymentService:
    """
    Handles payment completion for BOTH order payments and marketplace listing fee payments.
    Called by the Airpay webhook and the Mock payment endpoint.
    """
    @staticmethod
    def execute(order_id=None, provider_reference=None, metadata=None):
        try:
            with transaction.atomic():
                payment = None
                if provider_reference:
                    payment = Payment.objects.select_for_update().filter(provider_reference=provider_reference).first()
                
                if not payment and order_id:
                    # Try order-based lookup first, then marketplace product-based
                    payment = Payment.objects.select_for_update().filter(order_id=order_id).first()
                    if not payment:
                        payment = Payment.objects.select_for_update().filter(marketplace_product_id=order_id).first()
                
                if not payment:
                    raise ValueError(f"No payment found for Ref: {provider_reference} or Order: {order_id}")
                
                # Idempotency check: If already paid, just return it
                if payment.status in ['COLLECTED', 'RELEASE_READY', 'DISBURSED']:
                    return payment
                
                if success:
                    new_state = PaymentFSM.transition(payment.status, 'COLLECTED')
                    payment.status = new_state
                    payment.provider_reference = provider_reference
                    payment.rrn = rrn
                    payment.save()
                    
                    # --- Branch: Order Payment ---
                    if payment.order:
                        order = payment.order
                        order.is_paid = True
                        order.save(update_fields=['is_paid'])
                        
                        # Ledger Entry: Customer -> Platform (Held in Escrow)
                        Transaction.objects.create(
                            payment=payment,
                            from_wallet=order.customer,
                            to_wallet=None,
                            amount=payment.amount,
                            transaction_type='customer_to_platform',
                            status='COMPLETED'
                        )
                        
                        EventBus.publish(PaymentCollected(
                            aggregate_id=payment.id,
                            payload={'order_id': str(order.id)}
                        ))
                    
                    # --- Branch: Marketplace Listing Fee Payment ---
                    elif payment.marketplace_product:
                        from domain.services.marketplace_services import MarketplaceService
                        product = payment.marketplace_product
                        MarketplaceService.activate_product(
                            product_id=str(product.id),
                            actor=product.seller
                        )
                        
                        # Ledger Entry: Seller -> Platform (Listing Fee)
                        Transaction.objects.create(
                            payment=payment,
                            from_wallet=product.seller,
                            to_wallet=None,
                            amount=payment.amount,
                            transaction_type='seller_listing_fee',
                            status='COMPLETED'
                        )
                else:
                    new_state = PaymentFSM.transition(payment.status, 'FAILED')
                    payment.status = new_state
                    payment.save()
                    
                return payment
        except Exception as e:
            # Re-raise for the view to handle, but with context
            import logging
            logging.getLogger(__name__).error(f"CompletePaymentService failed: {str(e)}")
            raise


class ReleasePaymentService:
    """
    Escrow Release: Distributes funds to Restaurant and Driver using Airpay Split Settlement.
    """
    @staticmethod
    def execute(payment_id):
        from domain.services.wallet_services import WalletDomainService
        with transaction.atomic():
            payment = Payment.objects.select_for_update().select_related(
                'order', 
                'order__restaurant__owner', 
                'order__driver__user'
            ).get(id=payment_id)
            
            order = payment.order
            gateway = AirpayGateway()
            
            # Define Splits
            restaurant_payout = Decimal(str(order.items_subtotal)) * Decimal('0.90')
            driver_payout = Decimal(str(order.delivery_fee)) if order.driver else Decimal('0')
            platform_cut = payment.amount - restaurant_payout - driver_payout

            splits = [
                {'identifier': 'PLATFORM_ESCROW', 'value': float(platform_cut)},
                {'identifier': order.restaurant.owner.phone, 'value': float(restaurant_payout)},
            ]
            
            if order.driver:
                splits.append({'identifier': order.driver.user.phone, 'value': float(driver_payout)})

            # Trigger Airpay Split Settlement
            response = gateway.split_settlement(
                airpay_id=payment.provider_reference,
                rrn=payment.rrn,
                splits=splits
            )
            
            if response and response.get('success'):
                payment.status = PaymentFSM.transition(payment.status, 'DISBURSED')
                payment.save()
                
                # 1. Credit Restaurant Wallet
                WalletDomainService.credit(
                    order.restaurant.owner, 
                    restaurant_payout, 
                    f"Earnings from Order #{order.id}"
                )
                
                # 2. Credit Driver Wallet
                if order.driver:
                    WalletDomainService.credit(
                        order.driver.user, 
                        driver_payout, 
                        f"Delivery fee for Order #{order.id}"
                    )
                
                # Ledger transactions
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
                    error_code=response.get('error_code', 'UNKNOWN'),
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


