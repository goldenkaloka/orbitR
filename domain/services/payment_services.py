from apps.payments.models import Payment, Transaction, FailedPayout
from django.db import transaction
from decimal import Decimal
from django.utils import timezone
from apps.orders.models import Order
from domain.fsm.payment_fsm import PaymentFSM
from infrastructure.payment_gateways.factory import get_payment_gateway
from domain.events.bus import EventBus
from domain.events.types import PaymentCollected, PaymentInitiated, PaymentDisbursed, PaymentRefunded

class InitiatePaymentService:
    @staticmethod
    def execute(order_id=None, pool_member_id=None, provider=None, phone=None, bank_code=None):
        with transaction.atomic():
            from apps.orders.models import PoolMember
            
            # Identify the target and amount
            target_amount = 0
            order = None
            pool_member = None
            customer_phone = phone
            
            if order_id:
                order = Order.objects.select_for_update().get(id=order_id)
                target_amount = order.total_amount
                customer_phone = customer_phone or order.customer.phone
            elif pool_member_id:
                pool_member = PoolMember.objects.select_for_update().get(id=pool_member_id)
                target_amount = pool_member.items_subtotal + pool_member.delivery_fee_share
                customer_phone = customer_phone or pool_member.user.phone
            else:
                raise ValueError("Either order_id or pool_member_id is required.")

            # Create or get Payment record with locking
            payment_defaults = {'amount': target_amount, 'status': 'INITIATED'}
            if order:
                payment, created = Payment.objects.select_for_update().get_or_create(
                    order=order,
                    defaults=payment_defaults
                )
            else:
                payment, created = Payment.objects.select_for_update().get_or_create(
                    pool_member=pool_member,
                    defaults=payment_defaults
                )

            # Prevent double payment (FSM check)
            if payment.status in ['COLLECTED', 'RELEASE_READY', 'DISBURSED']:
                raise ValueError("This has already been paid.")

            gateway = get_payment_gateway(provider=provider, phone=customer_phone)

            response = gateway.collect(
                amount=payment.amount,
                phone=customer_phone,
                order_reference=str(order.id if order else pool_member.id),
                provider=provider,
                bank_code=bank_code
            )

            
            # Update payment with provider info if returned
            if response.get('success'):
                # Extract reference from the synchronous push response
                payment.provider_reference = response.get('provider_reference') or response.get('transactionId')
                payment.save()
                
                EventBus.publish(PaymentInitiated(
                    aggregate_id=payment.id,
                    payload={
                        'order_id': str(order.id if order else pool_member.id),
                        'amount': float(payment.amount),
                        'gateway': 'Mock' if response.get('instant_success') else 'Airpay',
                        'provider_reference': payment.provider_reference
                    }
                ))

            # --- AUTO-COMPLETE FOR MOCK PAYMENTS ---
            if response.get('instant_success'):
                # Call CompletePaymentService to finalize the order immediately
                CompletePaymentService.execute(
                    order_id=str(order.id if order else pool_member.id),
                    provider_reference=payment.provider_reference,
                    success=True
                )
                # Refresh from DB to get the updated status for the return
                payment.refresh_from_db()
            
            return {
                'payment': payment,
                'gateway_response': response,
                'is_push': not response.get('instant_success') # Only signal push if not instant success
            }


class CompletePaymentService:
    """
    Handles payment completion for BOTH order payments and marketplace listing fee payments.
    Called by the Airpay webhook and the Mock payment endpoint.
    """
    @staticmethod
    def execute(order_id=None, provider_reference=None, success=True, rrn=None):
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
                        payment = Payment.objects.select_for_update().filter(pool_member_id=order_id).first()

                
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
                    # Bug 8 Fix: Do NOT save payment yet — update order first so both
                    # are committed atomically. If order FSM raises, payment rolls back too.
                    
                    # --- Branch: Order Payment ---
                    if payment.order:
                        order = payment.order
                        order.is_paid = True
                        from domain.fsm.order_fsm import OrderFSM
                        order.status = OrderFSM.transition(order.status, 'CREATED')
                        order.save(update_fields=['is_paid', 'status'])
                        
                        # Now save payment after order succeeded
                        payment.save()
                        
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
                        product = payment.marketplace_product
                        from domain.services.marketplace_services import MarketplaceService
                        MarketplaceService.activate_product(
                            product_id=str(product.id),
                            actor=product.seller
                        )
                        
                        payment.save()
                        
                        # Ledger Entry: Seller -> Platform (Listing Fee)
                        Transaction.objects.create(
                            payment=payment,
                            from_wallet=product.seller,
                            to_wallet=None,
                            amount=payment.amount,
                            transaction_type='listing_fee',
                            status='COMPLETED'
                        )

                    # --- Branch: Pool Member Payment ---
                    elif payment.pool_member:
                        payment.save()
                        member = payment.pool_member
                        member.status = 'PAID'
                        member.save()
                        
                        # Ledger Entry: Member -> Platform (Escrow)
                        Transaction.objects.create(
                            payment=payment,
                            from_wallet=member.user,
                            to_wallet=None,
                            amount=payment.amount,
                            transaction_type='pool_member_to_platform',
                            status='COMPLETED'
                        )
                        
                        # Notify pool
                        from apps.events.utils import broadcast_pool_status
                        broadcast_pool_status(str(member.pool_id), member.pool.status, message=f"{member.user.username} has paid their share!")

                        # 4. Auto-Finalize if everyone has paid
                        pool = member.pool
                        if not pool.members.exclude(status='PAID').exists():
                            from domain.services.order_services import PoolOrderService
                            transaction.on_commit(lambda: PoolOrderService.finalize_pool(pool.id))
                    else:
                        # No order or product — just save payment status
                        payment.save()
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
            
            # Identify target order and calculate payouts
            if payment.order:
                order = payment.order
                restaurant_payout = Decimal(str(order.items_subtotal)) * Decimal('0.90')
                driver_payout = Decimal(str(order.delivery_fee)) if order.driver else Decimal('0')
            elif payment.pool_member:
                member = payment.pool_member
                # For group orders, we release the member's specific share
                restaurant_payout = Decimal(str(member.items_subtotal)) * Decimal('0.90')
                driver_payout = Decimal(str(member.delivery_fee_share))
                # Find the master order created from this pool
                from apps.orders.models import Order
                order = Order.objects.filter(pool=member.pool).order_by('-created_at').first()
                if not order:
                    raise ValueError(f"No master order found for pool {member.pool_id}")
            else:
                raise ValueError("Payment has no associated order or pool member.")

            gateway = get_payment_gateway()
            platform_cut = payment.amount - restaurant_payout - driver_payout

            # Define Splits
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
                
            gateway = get_payment_gateway()
            
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


