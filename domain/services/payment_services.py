from decimal import Decimal

from apps.orders.models import Order
from apps.payments.models import FailedPayout, Payment, Transaction
from django.db import transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import (
    PaymentCollected,
    PaymentDisbursed,
    PaymentFailed,
    PaymentInitiated,
    PaymentRefunded,
)
from domain.fsm.payment_fsm import PaymentFSM
from infrastructure.payment_gateways.factory import get_payment_gateway


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
                
                transaction.on_commit(lambda: EventBus.publish(PaymentInitiated(
                    aggregate_id=payment.id,
                    payload={
                        'payable_type': payable_type,
                        'payable_id': str(payable_id),
                        'amount': float(payment.amount),
                        'provider_reference': payment.provider_reference
                    }
                )))

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
                'is_push': response.get('success') and not response.get('instant_success')
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
                
                if payment.status in ['COLLECTED', 'DISBURSED', 'FAILED']:
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
                    
                    transaction.on_commit(lambda: EventBus.publish(PaymentCollected(
                        aggregate_id=payment.id,
                        payload={'payable_type': payment.payable_type, 'payable_id': str(payment.payable_id)}
                    )))
                else:
                    payment.status = PaymentFSM.transition(payment.status, 'FAILED')
                    payment.save()
                    
                    # Fix Issue 9: Publish event on failure
                    transaction.on_commit(lambda: EventBus.publish(PaymentFailed(
                        aggregate_id=payment.id,
                        payload={'payable_type': payment.payable_type, 'payable_id': str(payment.payable_id), 'error': 'Payment collection failed'}
                    )))
                    
                return payment
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"CompletePaymentService failed: {str(e)}")
            raise

    @staticmethod
    def _handle_order_payment(payment):
        from domain.fsm.order_fsm import OrderFSM
        from domain.services.orders.order_placement import ConfirmOrderService

        order = Order.objects.select_for_update().get(id=payment.payable_id)
        order.is_paid = True
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

        # Confirm synchronously so the order is reflected immediately,
        # not waiting on Celery event dispatch
        ConfirmOrderService.execute(order.id, order.customer)

    @staticmethod
    def _handle_pool_payment(payment):
        from apps.orders.models import OrderPool, PoolMember
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
        _pid = str(pool.id)
        _pstatus = pool.status
        _username = member.user.username

        all_paid = not pool.members.exclude(status='PAID').exists()
        _pool_id = str(pool.id)

        transaction.on_commit(lambda: broadcast_pool_status(
            _pid, _pstatus, message=f"{_username} has paid their share!"
        ))

        if all_paid:
            from domain.services.order_services import PoolOrderService
            transaction.on_commit(lambda pid=_pool_id: PoolOrderService.finalize_pool(pid))

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
            vendor_phone = None
            driver_phone = None

            if payment.payable_type == 'order':
                order = Order.objects.select_related(
                    'restaurant', 'restaurant__owner', 'batch', 'driver', 'driver__user'
                ).get(id=payment.payable_id)
                commission = order.restaurant.commission_rate if order.restaurant else Decimal('0.100')
                restaurant_payout = Decimal(str(order.items_subtotal or 0)) * (Decimal('1.0') - commission)

                if order.batch and order.batch.driver_payout:
                    batch_order_count = order.batch.orders.count()
                    driver_payout = order.batch.driver_payout / batch_order_count if batch_order_count > 0 else Decimal('0')
                elif order.driver:
                    from domain.services.pricing_service import PricingService
                    policy = PricingService.get_active_policy()
                    driver_share = Decimal('0.90')
                    if policy and policy.single_order_driver_share:
                        driver_share = policy.single_order_driver_share
                    driver_payout = Decimal(str(order.delivery_fee or 0)) * driver_share
                else:
                    driver_payout = Decimal('0')

                if order.restaurant and order.restaurant.owner:
                    vendor_phone = order.restaurant.owner.phone
                if order.driver and order.driver.user:
                    driver_phone = order.driver.user.phone

            elif payment.payable_type == 'pool_member':
                from apps.orders.models import PoolMember
                member = PoolMember.objects.select_related(
                    'pool', 'pool__restaurant', 'pool__restaurant__owner', 'pool__batch'
                ).get(id=payment.payable_id)
                commission = member.pool.restaurant.commission_rate if member.pool.restaurant else Decimal('0.100')
                restaurant_payout = Decimal(str(member.items_subtotal or 0)) * (Decimal('1.0') - commission)
                
                # Align driver payout with the batch payout split equally among paid pool members
                if member.pool.batch and member.pool.batch.driver_payout:
                    paid_member_count = member.pool.members.filter(status='PAID').count()
                    driver_payout = member.pool.batch.driver_payout / paid_member_count if paid_member_count > 0 else Decimal('0')
                else:
                    driver_payout = Decimal(str(member.delivery_fee_share or 0))

                order = Order.objects.filter(pool=member.pool).order_by('-created_at').first()
                if order and order.restaurant and order.restaurant.owner:
                    vendor_phone = order.restaurant.owner.phone
                if order and order.driver and order.driver.user:
                    driver_phone = order.driver.user.phone

            if not order:
                raise ValueError("No payout target found for this payment.")
            if not vendor_phone:
                raise ValueError(f"Cannot process payment: Restaurant has no owner phone number")

            # Helper to check if TZ phone number is valid
            def is_valid_tz_phone(phone_number):
                if not phone_number:
                    return False
                p = str(phone_number).replace('+', '').strip()
                if p.startswith('255'): p = p[3:]
                if p.startswith('0'): p = p[1:]
                if not p.isdigit() or len(p) != 9:
                    return False
                prefix = p[:2]
                valid_prefixes = {'74', '75', '76', '73', '65', '67', '71', '72', '68', '69', '78', '79', '61', '62'}
                return prefix in valid_prefixes

            platform_cut = payment.amount - restaurant_payout - driver_payout
            if platform_cut < 0:
                platform_cut = Decimal('0')

            actual_restaurant_payout = restaurant_payout
            actual_driver_payout = driver_payout

            # Validate restaurant owner phone
            if restaurant_payout > 0 and vendor_phone:
                if not is_valid_tz_phone(vendor_phone):
                    import logging
                    logging.getLogger(__name__).warning(
                        f"Vendor phone number '{vendor_phone}' for payment {payment.id} is invalid. "
                        f"Redirecting restaurant share of {restaurant_payout} to platform primary account."
                    )
                    platform_cut += restaurant_payout
                    actual_restaurant_payout = Decimal('0')

            # Validate driver phone
            if driver_payout > 0 and driver_phone:
                if not is_valid_tz_phone(driver_phone):
                    import logging
                    logging.getLogger(__name__).warning(
                        f"Driver phone number '{driver_phone}' for payment {payment.id} is invalid. "
                        f"Redirecting driver share of {driver_payout} to platform primary account."
                    )
                    platform_cut += driver_payout
                    actual_driver_payout = Decimal('0')

            # Build split payload per Airpay docs:
            # PRIACCT_IDENTIFIER = platform primary account
            # OTHER_IDENTIFIERS = vendor phone | driver phone (pipe-separated)
            # SPLIT_VALUES = vendor amount | driver amount (pipe-separated)
            primary_account = get_payment_gateway().config.get('PRIMARY_ACCOUNT')
            if not primary_account:
                raise ValueError("AIRPAY_PRIMARY_ACCOUNT not configured")

            other_identifiers = []
            split_values = []

            if actual_restaurant_payout > 0 and vendor_phone:
                other_identifiers.append(vendor_phone)
                split_values.append(float(actual_restaurant_payout))

            if actual_driver_payout > 0 and driver_phone:
                other_identifiers.append(driver_phone)
                split_values.append(float(actual_driver_payout))

            # Generate deterministic 15-character split_request_id for gateway idempotency
            import hashlib
            h = hashlib.sha256(str(payment.id).encode())
            split_request_id = str(int(h.hexdigest(), 16))[-15:]

            gateway = get_payment_gateway()
            response = gateway.split_settlement(
                airpay_id=payment.provider_reference,
                rrn=payment.rrn,
                primary_account=primary_account,
                primary_value=float(platform_cut),
                other_identifiers=other_identifiers,
                split_values=split_values,
                split_request_id=split_request_id,
            )

            if response and response.get('success'):
                payment.status = PaymentFSM.transition(payment.status, 'DISBURSED')
                payment.save()

                # Credit vendor wallet proportionally by stall/section
                if restaurant_payout > 0 and order.restaurant and order.restaurant.owner:
                    ReleasePaymentService._distribute_restaurant_payout(order, restaurant_payout)
                    Transaction.objects.create(
                        payment=payment,
                        from_wallet=None,
                        to_wallet=order.restaurant.owner,
                        amount=restaurant_payout,
                        transaction_type='platform_to_restaurant',
                        status='COMPLETED'
                    )

                # Credit driver wallet
                if driver_payout > 0 and order.driver and order.driver.user:
                    WalletDomainService.credit(
                        order.driver.user,
                        driver_payout,
                        f"Delivery fee for Order #{order.id}"
                    )
                    Transaction.objects.create(
                        payment=payment,
                        from_wallet=None,
                        to_wallet=order.driver.user,
                        amount=driver_payout,
                        transaction_type='platform_to_driver',
                        status='COMPLETED'
                    )

                transaction.on_commit(lambda: EventBus.publish(PaymentDisbursed(
                    aggregate_id=payment.id,
                    payload={
                        'order_id': str(order.id),
                        'restaurant_payout': str(restaurant_payout),
                        'driver_payout': str(driver_payout),
                    }
                )))
            else:
                error_msg = response.get('error', 'Unknown Error') if response else 'No response from gateway'
                error_code = response.get('error_code', 'UNKNOWN') if response else 'NO_RESPONSE'
                FailedPayout.objects.create(
                    payment=payment,
                    to_wallet=order.restaurant.owner,
                    amount=restaurant_payout,
                    transaction_type='split_settlement_failure',
                    reason=error_msg,
                    error_code=error_code,
                    retry_count=0
                )
                logger.error(f"Split settlement failed for payment {payment.id}: [{error_code}] {error_msg}")

            return payment

    @staticmethod
    def _distribute_restaurant_payout(order, restaurant_payout):
        """
        Distributes the restaurant payout among the sections (stalls) that prepared the items.
        If any items have no section, their share goes to the restaurant owner's wallet.
        """
        from decimal import Decimal

        from apps.wallets.models import Wallet
        from domain.services.wallet_services import WalletDomainService

        items = list(order.items.select_related('food', 'section').all())
        if not items:
            WalletDomainService.credit(order.restaurant.owner, restaurant_payout, f"Earnings from Order #{order.id}")
            return

        total_items_price = sum(item.price for item in items)
        if total_items_price == 0:
            WalletDomainService.credit(order.restaurant.owner, restaurant_payout, f"Earnings from Order #{order.id}")
            return

        distributed_amount = Decimal('0')
        cumulative_price = Decimal('0')
        for idx, item in enumerate(items):
            cumulative_price += Decimal(str(item.price))
            if total_items_price > 0:
                cumulative_target = (restaurant_payout * cumulative_price / total_items_price).quantize(Decimal('0.01'))
            else:
                cumulative_target = Decimal('0')
            
            item_payout = cumulative_target - distributed_amount
            distributed_amount += item_payout

            if item_payout <= 0:
                continue

            if item.section:
                wallet, _ = Wallet.objects.get_or_create(section=item.section)
                WalletDomainService.credit_wallet(wallet, item_payout, f"Earnings for {item.food.name if item.food else 'item'} from Order #{order.id}")
            else:
                WalletDomainService.credit(order.restaurant.owner, item_payout, f"Earnings for {item.food.name if item.food else 'item'} from Order #{order.id}")

class RefundPaymentService:
    @staticmethod
    def execute(payable_type, payable_id, override_amount: Decimal = None):
        with transaction.atomic():
            payments = Payment.objects.select_for_update().filter(
                payable_type=payable_type,
                payable_id=payable_id,
                status__in=['COLLECTED', 'ON_HOLD'],
            )
            
            if not payments.exists():
                # Legacy fallback for old callers passing order_id only
                if payable_type == 'order':
                    payments = Payment.objects.select_for_update().filter(
                        order_id=payable_id, status__in=['COLLECTED', 'ON_HOLD'],
                    )

            gateway = get_payment_gateway()
            
            for payment in payments:
                refund_amount = override_amount if override_amount is not None else payment.amount
                response = gateway.refund(transaction_id=payment.provider_reference, amount=refund_amount)
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
                            amount=refund_amount,
                            transaction_type='platform_to_customer_refund',
                            status='COMPLETED'
                        )
                    
                    # Use a function to capture the current payment in the closure
                    def publish_refund(p=payment, amt=refund_amount):
                        EventBus.publish(PaymentRefunded(
                            aggregate_id=p.id, 
                            payload={'type': p.payable_type, 'payable_id': str(p.payable_id), 'refund_amount': str(amt)}
                        ))
                    transaction.on_commit(publish_refund)
            
            return True


