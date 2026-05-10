from apps.payments.models import Payment, Transaction, FailedPayout
from django.db import transaction
from decimal import Decimal
from django.utils import timezone
from apps.orders.models import Order
from domain.fsm.payment_fsm import PaymentFSM
from infrastructure.payment_gateways.factory import get_payment_gateway
from domain.events.bus import EventBus
from domain.events.types import PaymentCollected, PaymentInitiated, PaymentDisbursed, PaymentRefunded, PaymentFailed

class PaymentTargetResolver:
    @staticmethod
    def resolve(payable_type, payable_id):
        if payable_type == 'order':
            order = Order.objects.get(id=payable_id)
            return {
                'amount': order.total_amount,
                'phone': order.customer.phone,
                'user': order.customer,
                'description': f"Payment for Order #{order.id}"
            }
        elif payable_type == 'pool_member':
            from apps.orders.models import PoolMember
            member = PoolMember.objects.get(id=payable_id)
            return {
                'amount': member.items_subtotal + member.delivery_fee_share,
                'phone': member.user.phone,
                'user': member.user,
                'description': f"Pool Member Share - {member.user.username}"
            }
        elif payable_type == 'marketplace_product':
            from apps.marketplace.models import MarketplaceProduct
            product = MarketplaceProduct.objects.get(id=payable_id)
            return {
                'amount': product.calculate_listing_fee(),
                'phone': product.seller.phone,
                'user': product.seller,
                'description': f"Listing Fee for {product.name}"
            }
        else:
            raise ValueError(f"Unknown payable_type: {payable_type}")

class InitiatePaymentService:
    @staticmethod
    def execute(payable_type, payable_id, provider=None, phone=None, bank_code=None):
        with transaction.atomic():
            # 1. Resolve target details
            target = PaymentTargetResolver.resolve(payable_type, payable_id)
            customer_phone = phone or target['phone']
            
            # 2. Create or get Payment record with locking
            payment, created = Payment.objects.select_for_update().get_or_create(
                payable_type=payable_type,
                payable_id=payable_id,
                defaults={'amount': target['amount'], 'status': 'INITIATED'}
            )

            # Fix Issue 4: Reset status to INITIATED if we are retrying a FAILED payment
            if not created and payment.status == 'FAILED':
                payment.status = 'INITIATED'
                payment.save()

            # Sync legacy fields for backward compatibility
            if not payment.order and payable_type == 'order': payment.order_id = payable_id
            if not payment.pool_member and payable_type == 'pool_member': payment.pool_member_id = payable_id
            if not payment.marketplace_product and payable_type == 'marketplace_product': payment.marketplace_product_id = payable_id
            payment.save()

            # 3. Prevent double payment
            if payment.status in ['COLLECTED', 'DISBURSED']:
                raise ValueError("This has already been paid.")

            # 4. Initiate with Gateway
            gateway = get_payment_gateway(provider=provider, phone=customer_phone)
            response = gateway.collect(
                amount=payment.amount,
                phone=customer_phone,
                order_reference=str(payment.id), # Use Payment ID as gateway reference
                provider=provider,
                bank_code=bank_code
            )

            if response.get('success'):
                payment.provider_reference = response.get('provider_reference') or response.get('transactionId')
                payment.save()
                
                EventBus.publish(PaymentInitiated(
                    aggregate_id=payment.id,
                    payload={
                        'payable_type': payable_type,
                        'payable_id': str(payable_id),
                        'amount': float(payment.amount),
                        'provider_reference': payment.provider_reference
                    }
                ))

            # --- AUTO-COMPLETE FOR MOCK ---
            if response.get('instant_success'):
                CompletePaymentService.execute(
                    payment_id=payment.id,
                    provider_reference=payment.provider_reference,
                    success=True
                )
                payment.refresh_from_db()
            
            return {
                'payment': payment,
                'gateway_response': response,
                'is_push': not response.get('instant_success')
            }

class CompletePaymentService:
    @staticmethod
    def execute(payment_id=None, provider_reference=None, success=True, rrn=None, reference_id=None):
        """
        Unfied completion handler. Uses Strategy Pattern for post-payment logic.
        """
        try:
            with transaction.atomic():
                payment = None
                if payment_id:
                    payment = Payment.objects.select_for_update().get(id=payment_id)
                elif provider_reference:
                    payment = Payment.objects.select_for_update().filter(provider_reference=provider_reference).first()
                elif reference_id:
                    # Fallback for old API callers
                    payment = Payment.objects.select_for_update().filter(payable_id=reference_id).first()
                
                if not payment:
                    raise ValueError(f"No payment found for Ref: {provider_reference} or ID: {payment_id}")
                
                if payment.status in ['COLLECTED', 'DISBURSED']:
                    import logging
                    logging.getLogger(__name__).warning(f"Payment {payment.id} already {payment.status}. Ignoring duplicate webhook.")
                    return payment
                
                if success:
                    payment.status = PaymentFSM.transition(payment.status, 'COLLECTED')
                    payment.provider_reference = provider_reference or payment.provider_reference
                    payment.rrn = rrn
                    
                    # DISPATCH TO HANDLER
                    if payment.payable_type == 'order':
                        CompletePaymentService._handle_order_payment(payment)
                    elif payment.payable_type == 'pool_member':
                        CompletePaymentService._handle_pool_payment(payment)
                    elif payment.payable_type == 'marketplace_product':
                        CompletePaymentService._handle_marketplace_payment(payment)
                    
                    payment.save()
                    
                    EventBus.publish(PaymentCollected(
                        aggregate_id=payment.id,
                        payload={'payable_type': payment.payable_type, 'payable_id': str(payment.payable_id)}
                    ))
                else:
                    payment.status = PaymentFSM.transition(payment.status, 'FAILED')
                    payment.save()
                    
                    # Fix Issue 9: Publish event on failure
                    EventBus.publish(PaymentFailed(
                        aggregate_id=payment.id,
                        payload={'payable_type': payment.payable_type, 'payable_id': str(payment.payable_id), 'error': 'Payment collection failed'}
                    ))
                    
                return payment
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"CompletePaymentService failed: {str(e)}")
            raise

    @staticmethod
    def _handle_order_payment(payment):
        order = Order.objects.get(id=payment.payable_id)
        order.is_paid = True
        from domain.fsm.order_fsm import OrderFSM
        order.status = OrderFSM.transition(order.status, 'CREATED')
        order.save(update_fields=['is_paid', 'status'])
        
        Transaction.objects.create(
            payment=payment,
            from_wallet=order.customer,
            to_wallet=None,
            amount=payment.amount,
            transaction_type='customer_to_platform',
            status='COMPLETED'
        )

    @staticmethod
    def _handle_pool_payment(payment):
        from apps.orders.models import PoolMember, OrderPool
        member = PoolMember.objects.get(id=payment.payable_id)
        pool = OrderPool.objects.select_for_update().get(id=member.pool_id)
        
        member.status = 'PAID'
        member.save()
        
        Transaction.objects.create(
            payment=payment,
            from_wallet=member.user,
            to_wallet=None,
            amount=payment.amount,
            transaction_type='pool_member_to_platform',
            status='COMPLETED'
        )
        
        from apps.events.utils import broadcast_pool_status
        broadcast_pool_status(str(pool.id), pool.status, message=f"{member.user.username} has paid their share!")

        if not pool.members.exclude(status='PAID').exists():
            from domain.services.order_services import PoolOrderService
            PoolOrderService.finalize_pool(pool.id)

    @staticmethod
    def _handle_marketplace_payment(payment):
        from apps.marketplace.models import MarketplaceProduct
        from domain.services.marketplace_services import MarketplaceService
        product = MarketplaceProduct.objects.get(id=payment.payable_id)
        MarketplaceService.activate_product(product_id=str(product.id), actor=product.seller)
        
        Transaction.objects.create(
            payment=payment,
            from_wallet=product.seller,
            to_wallet=None,
            amount=payment.amount,
            transaction_type='listing_fee',
            status='COMPLETED'
        )

class ReleasePaymentService:
    @staticmethod
    def execute(payment_id):
        from domain.services.wallet_services import WalletDomainService
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(id=payment_id)
            
            # Resolve payout targets
            order = None
            restaurant_payout = Decimal('0')
            driver_payout = Decimal('0')

            if payment.payable_type == 'order':
                order = Order.objects.get(id=payment.payable_id)
                restaurant_payout = Decimal(str(order.items_subtotal or 0)) * Decimal('0.90')
                driver_payout = Decimal(str(order.delivery_fee or 0)) if order.driver else Decimal('0')
            elif payment.payable_type == 'pool_member':
                from apps.orders.models import PoolMember
                member = PoolMember.objects.get(id=payment.payable_id)
                restaurant_payout = Decimal(str(member.items_subtotal or 0)) * Decimal('0.90')
                driver_payout = Decimal(str(member.delivery_fee_share or 0))
                order = member.pool.final_order
                if not order:
                    # Fallback for pools finalized before this change
                    order = Order.objects.filter(pool=member.pool).order_by('-created_at').first()
            
            if not order:
                raise ValueError("No payout target found for this payment.")

            gateway = get_payment_gateway()
            platform_cut = payment.amount - restaurant_payout - driver_payout
            if platform_cut < 0:
                # Emergency guard: don't let platform go negative due to rounding
                platform_cut = Decimal('0')

            splits = [
                {'identifier': 'PLATFORM_ESCROW', 'value': float(platform_cut)},
                {'identifier': order.restaurant.owner.phone, 'value': float(restaurant_payout)},
            ]
            if order.driver:
                splits.append({'identifier': order.driver.user.phone, 'value': float(driver_payout)})

            response = gateway.split_settlement(
                airpay_id=payment.provider_reference,
                rrn=payment.rrn,
                splits=splits
            )
            
            if response and response.get('success'):
                payment.status = PaymentFSM.transition(payment.status, 'DISBURSED')
                payment.save()
                
                WalletDomainService.credit(order.restaurant.owner, restaurant_payout, f"Earnings from Order #{order.id}")
                if order.driver:
                    WalletDomainService.credit(order.driver.user, driver_payout, f"Delivery for Order #{order.id}")
                
                Transaction.objects.create(payment=payment, to_wallet=order.restaurant.owner, amount=restaurant_payout, transaction_type='platform_to_restaurant', status='COMPLETED')
                if order.driver:
                    Transaction.objects.create(payment=payment, to_wallet=order.driver.user, amount=driver_payout, transaction_type='platform_to_driver', status='COMPLETED')
                
                EventBus.publish(PaymentDisbursed(aggregate_id=payment.id, payload={'order_id': str(order.id)}))
            else:
                FailedPayout.objects.create(payment=payment, to_wallet=order.restaurant.owner, amount=restaurant_payout, transaction_type='split_settlement_failure', reason=response.get('error', 'Unknown Error'))
            
            return payment

class RefundPaymentService:
    @staticmethod
    def execute(payable_type, payable_id):
        with transaction.atomic():
            payments = Payment.objects.select_for_update().filter(
                payable_type=payable_type,
                payable_id=payable_id,
                status='COLLECTED'
            )
            
            if not payments.exists():
                # Legacy fallback for old callers passing order_id only
                if payable_type == 'order':
                    payments = Payment.objects.select_for_update().filter(order_id=payable_id, status='COLLECTED')

            gateway = get_payment_gateway()
            
            for payment in payments:
                response = gateway.refund(airpay_id=payment.provider_reference, amount=payment.amount)
                if response and response.get('success'):
                    payment.status = PaymentFSM.transition(payment.status, 'REFUNDED')
                    payment.refund_id = response.get('refund_id')
                    payment.refunded_at = timezone.now()
                    payment.save()
                    
                    # Determine refund recipient
                    recipient = None
                    if payment.payable_type == 'order':
                        recipient = Order.objects.get(id=payment.payable_id).customer
                    elif payment.payable_type == 'pool_member':
                        from apps.orders.models import PoolMember
                        recipient = PoolMember.objects.get(id=payment.payable_id).user
                        
                    if recipient:
                        Transaction.objects.create(
                            payment=payment,
                            to_wallet=recipient,
                            amount=payment.amount,
                            transaction_type='platform_to_customer_refund',
                            status='COMPLETED'
                        )
            
            EventBus.publish(PaymentRefunded(aggregate_id=payment.id, payload={'type': payment.payable_type, 'payable_id': str(payment.payable_id)}))
            return True


