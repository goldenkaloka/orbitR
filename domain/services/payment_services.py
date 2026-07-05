import logging
import uuid
from decimal import Decimal

from apps.orders.models import Order
from apps.payments.models import Payment, Transaction
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

logger = logging.getLogger(__name__)


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
                'amount': member.items_subtotal + member.delivery_fee_share + member.service_fee_share,
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


def _can_pay_target(actor, target):
    if not actor or not actor.is_authenticated:
        return False
    if getattr(actor, 'is_staff', False) or getattr(actor, 'is_superuser', False):
        return True
    return target.get('user') == actor


class InitiatePaymentService:
    @staticmethod
    def execute(payable_type, payable_id, phone=None, bank_code=None, actor=None):
        with transaction.atomic():
            # Normalize payable_id to canonical UUID format (with hyphens)
            # to prevent duplicate payments due to inconsistent formatting
            try:
                payable_id = str(uuid.UUID(str(payable_id)))
            except (ValueError, AttributeError):
                pass

            # 1. Resolve target details
            target = PaymentTargetResolver.resolve(payable_type, payable_id)
            if actor is not None and not _can_pay_target(actor, target):
                raise ValueError("You are not allowed to initiate this payment.")
            customer_phone = phone or target['phone']
            
            # 2. Create or get Payment record with locking
            payment, created = Payment.objects.select_for_update().get_or_create(
                payable_type=payable_type,
                payable_id=payable_id,
                defaults={'amount': target['amount'], 'status': 'INITIATED'}
            )

            if not created and payment.status not in ['COLLECTED', 'DISBURSED']:
                if payment.amount != target['amount']:
                    payment.amount = target['amount']
                if payment.status == 'FAILED':
                    payment.status = 'INITIATED'

            # Sync legacy fields for backward compatibility
            if not payment.order and payable_type == 'order': payment.order_id = payable_id
            if not payment.pool_member and payable_type == 'pool_member': payment.pool_member_id = payable_id
            if not payment.marketplace_product and payable_type == 'marketplace_product': payment.marketplace_product_id = payable_id
            payment.save()

            # 3. Prevent double payment
            if payment.status in ['COLLECTED', 'DISBURSED']:
                raise ValueError("This has already been paid.")

            # 4. Initiate with Gateway
            gateway = get_payment_gateway(phone=customer_phone)
            response = gateway.collect(
                amount=target['amount'],
                phone=customer_phone,
                order_reference=str(payment.id), # Use Payment ID as gateway reference
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

PLATFORM_USER_PHONE = '+255000000000'


def get_platform_user():
    """Get or create the internal platform user for double-entry accounting."""
    from apps.users.models import User
    user, created = User.objects.get_or_create(
        phone=PLATFORM_USER_PHONE,
        defaults={'username': 'platform', 'role': 'admin'}
    )
    if created:
        logger.info("Created platform user for double-entry accounting")
    return user


def _get_platform_wallet():
    """Get or create the platform wallet used for escrow tracking."""
    from apps.wallets.models import Wallet
    wallet, _ = Wallet.objects.select_for_update().get_or_create(user=get_platform_user())
    return wallet


def _hold_escrow(amount: Decimal):
    """Record that amount is expected from gateway (escrow hold)."""
    wallet = _get_platform_wallet()
    wallet.pending_balance += amount
    wallet.save()


def _release_escrow(total_amount: Decimal, platform_cut: Decimal):
    """Move amount from escrow to cleared balance on disbursement."""
    wallet = _get_platform_wallet()
    wallet.pending_balance -= total_amount
    wallet.balance += platform_cut
    wallet.save()


def _record_platform_revenue(amount: Decimal, description: str):
    """Record platform-owned money that is not held for a future payout."""
    from apps.wallets.models import WalletTransaction
    wallet = _get_platform_wallet()
    wallet.balance += amount
    wallet.save()
    WalletTransaction.objects.create(
        wallet=wallet,
        amount=amount,
        tx_type='CREDIT',
        description=description,
    )


def _release_escrow_refund(amount: Decimal, cancel_fee: Decimal):
    """On refund: release full amount from escrow, keep cancel fee as earned."""
    wallet = _get_platform_wallet()
    wallet.pending_balance -= amount
    if cancel_fee > 0:
        wallet.balance += cancel_fee
    wallet.save()


def _release_escrow_disbursed_refund(payment):
    """On refund of a disbursed payment: reverse the platform_cut from balance."""
    from apps.payments.models import Transaction as PmtTransaction
    platform_entry = PmtTransaction.objects.filter(
        payment=payment,
        transaction_type='platform_revenue',
    ).first()
    if platform_entry and platform_entry.amount > 0:
        wallet = _get_platform_wallet()
        wallet.balance -= platform_entry.amount
        wallet.save()


def _create_double_entry(payment, from_user, to_user, amount, entry_type, status='COMPLETED'):
    """
    Create both a Transaction (User-based, backward compat) and a LedgerEntry (Wallet-based, double-entry).
    """
    txn = Transaction.objects.create(
        payment=payment,
        from_wallet=from_user,
        to_wallet=to_user,
        amount=amount,
        transaction_type=entry_type,
        status=status,
    )
    # Also create the Wallet-based double-entry record
    from apps.payments.models import LedgerEntry
    from apps.wallets.models import Wallet
    from_debit = Wallet.objects.get_or_create(user=from_user)[0] if from_user else None
    to_credit = Wallet.objects.get_or_create(user=to_user)[0] if to_user else None
    LedgerEntry.objects.create(
        payment=payment,
        debit_wallet=from_debit,
        credit_wallet=to_credit,
        amount=amount,
        entry_type=entry_type,
    )
    return txn


def _refunded_principal_total(payment) -> Decimal:
    """Amount of the original payment already removed from escrow by refunds."""
    total = Decimal('0')
    for txn in payment.transactions.filter(
        transaction_type__in=['platform_to_customer_refund', 'platform_cancel_fee']
    ).only('amount'):
        total += Decimal(str(txn.amount or 0))
    return total


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
                    logger.warning(
                        f"Payment {payment.id} already {payment.status} (likely webhook retry). "
                        f"Ignoring duplicate. provider_ref={provider_reference}"
                    )
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
            logger.error(f"CompletePaymentService failed: {str(e)}")
            raise

    @staticmethod
    def _handle_order_payment(payment):
        from domain.fsm.order_fsm import OrderFSM
        from domain.services.orders.order_placement import ConfirmOrderService

        order = Order.objects.select_for_update().get(id=payment.payable_id)
        order.is_paid = True
        order.status = OrderFSM.transition(order.status, 'CREATED')
        order.save(update_fields=['is_paid', 'status'])

        _create_double_entry(
            payment=payment,
            from_user=order.customer,
            to_user=get_platform_user(),
            amount=payment.amount,
            entry_type='customer_to_platform',
        )

        # Escrow hold: record money expected from the gateway
        _hold_escrow(payment.amount)

        # Confirm synchronously so the order is reflected immediately,
        # not waiting on Celery event dispatch
        ConfirmOrderService.execute(order.id, order.customer)

    @staticmethod
    def _handle_pool_payment(payment):
        from apps.orders.models import OrderPool, PoolMember
        member = PoolMember.objects.select_for_update().get(id=payment.payable_id)
        pool = OrderPool.objects.select_for_update().get(id=member.pool_id)
        
        member.status = 'PAID'
        member.save()
        
        _create_double_entry(
            payment=payment,
            from_user=member.user,
            to_user=get_platform_user(),
            amount=payment.amount,
            entry_type='pool_member_to_platform',
        )

        # Escrow hold: record money expected from the gateway
        _hold_escrow(payment.amount)

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
        
        _create_double_entry(
            payment=payment,
            from_user=product.seller,
            to_user=get_platform_user(),
            amount=payment.amount,
            entry_type='listing_fee',
        )
        _record_platform_revenue(payment.amount, f"Marketplace listing fee for {product.id}")
        _create_double_entry(
            payment=payment,
            from_user=get_platform_user(),
            to_user=get_platform_user(),
            amount=payment.amount,
            entry_type='platform_revenue',
        )

class ReleasePaymentService:
    @staticmethod
    def execute(payment_id):
        from domain.services.wallet_services import WalletDomainService
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(id=payment_id)

            if payment.status == 'DISBURSED':
                logger.warning(f"Payment {payment.id} already disbursed; skipping release.")
                return payment

            # Resolve payout targets
            order = None
            restaurant_payout = Decimal('0')
            driver_payout = Decimal('0')

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

            elif payment.payable_type == 'pool_member':
                from apps.orders.models import OrderPool, PoolMember
                member = PoolMember.objects.select_related(
                    'pool', 'pool__restaurant', 'pool__restaurant__owner', 'pool__batch'
                ).get(id=payment.payable_id)
                OrderPool.objects.select_for_update().get(id=member.pool_id)
                commission = member.pool.restaurant.commission_rate if member.pool.restaurant else Decimal('0.100')
                restaurant_payout = Decimal(str(member.items_subtotal or 0)) * (Decimal('1.0') - commission)

                if member.pool.batch and member.pool.batch.driver_payout:
                    paid_member_count = member.pool.members.filter(status='PAID').count()
                    driver_payout = member.pool.batch.driver_payout / paid_member_count if paid_member_count > 0 else Decimal('0')
                else:
                    driver_payout = Decimal(str(member.delivery_fee_share or 0))

                order = Order.objects.filter(pool=member.pool).order_by('-created_at').first()

            if not order:
                raise ValueError("No payout target found for this payment.")

            refunded_principal = _refunded_principal_total(payment)
            net_collected_amount = payment.amount - refunded_principal
            if net_collected_amount < 0:
                raise ValueError("Refunded amount exceeds collected payment amount.")

            platform_cut = net_collected_amount - restaurant_payout - driver_payout
            if platform_cut < 0:
                raise ValueError(
                    "Cannot release payment because restaurant and driver payouts exceed collected amount."
                )

            _platform = get_platform_user()
            payment.status = PaymentFSM.transition(payment.status, 'DISBURSED')
            payment.save()

            # Release escrow
            _release_escrow(net_collected_amount, platform_cut)

            # Credit wallets (ledger only — no real money moves)
            has_section_items = order.items.filter(section__isnull=False).exists() if order else False
            if restaurant_payout > 0 and order.restaurant:
                if has_section_items:
                    _distribute_restaurant_payout(order, restaurant_payout)
                elif order.restaurant.owner:
                    WalletDomainService.credit(
                        user=order.restaurant.owner,
                        amount=restaurant_payout,
                        description=f'Order payout — #{str(order.id)[:8]}',
                    )

            if driver_payout > 0 and order.driver and order.driver.user:
                WalletDomainService.credit(
                    user=order.driver.user,
                    amount=driver_payout,
                    description=f'Delivery payout — #{str(order.id)[:8]}',
                )

            # Double-entry accounting records
            if restaurant_payout > 0 and order.restaurant and order.restaurant.owner:
                _create_double_entry(
                    payment=payment,
                    from_user=_platform,
                    to_user=order.restaurant.owner,
                    amount=restaurant_payout,
                    entry_type='platform_to_restaurant',
                )

            if driver_payout > 0 and order.driver and order.driver.user:
                _create_double_entry(
                    payment=payment,
                    from_user=_platform,
                    to_user=order.driver.user,
                    amount=driver_payout,
                    entry_type='platform_to_driver',
                )

            if platform_cut > 0:
                _create_double_entry(
                    payment=payment,
                    from_user=_platform,
                    to_user=_platform,
                    amount=platform_cut,
                    entry_type='platform_revenue',
                )

            transaction.on_commit(lambda: EventBus.publish(PaymentDisbursed(
                aggregate_id=payment.id,
                payload={
                    'order_id': str(order.id),
                    'restaurant_payout': str(restaurant_payout),
                    'driver_payout': str(driver_payout),
                }
            )))

            return payment


def _distribute_restaurant_payout(order, restaurant_payout):
    """Distribute restaurant payout proportionally to section (stall) wallets."""
    from apps.wallets.models import Wallet
    from domain.services.wallet_services import WalletDomainService

    items = list(order.items.select_related('food', 'section').all())
    if not items:
        return

    total_items_price = sum(item.price for item in items)
    if total_items_price == 0:
        return

    distributed_amount = Decimal('0')
    cumulative_price = Decimal('0')
    for item in items:
        cumulative_price += Decimal(str(item.price))
        cumulative_target = (restaurant_payout * cumulative_price / total_items_price).quantize(Decimal('0.01'))
        item_payout = cumulative_target - distributed_amount
        distributed_amount += item_payout

        if item_payout <= 0 or not item.section:
            continue

        wallet, _ = Wallet.objects.get_or_create(section=item.section)
        WalletDomainService.credit_wallet(wallet, item_payout, f"Earnings for {item.food.name if item.food else 'item'} from Order #{order.id}")

class RefundPaymentService:
    @staticmethod
    def execute(payable_type, payable_id, override_amount: Decimal = None, cancel_fee_pct: Decimal = Decimal('0')):
        with transaction.atomic():
            payments = Payment.objects.select_for_update().filter(
                payable_type=payable_type,
                payable_id=payable_id,
                status__in=['COLLECTED', 'ON_HOLD', 'DISBURSED', 'PARTIALLY_REFUNDED'],
            )
            
            if not payments.exists():
                # Legacy fallback for old callers passing order_id only
                if payable_type == 'order':
                    payments = Payment.objects.select_for_update().filter(
                        order_id=payable_id, status__in=['COLLECTED', 'ON_HOLD', 'DISBURSED', 'PARTIALLY_REFUNDED'],
                    )

            gateway = get_payment_gateway()
            
            for payment in payments:
                already_refunded = _refunded_principal_total(payment)
                remaining_amount = payment.amount - already_refunded
                if remaining_amount <= 0:
                    continue

                base_amount = override_amount if override_amount is not None else remaining_amount
                if base_amount <= 0:
                    continue
                if base_amount > remaining_amount:
                    raise ValueError("Refund amount cannot exceed the remaining refundable payment amount.")
                cancel_fee = base_amount * cancel_fee_pct
                refund_amount = base_amount - cancel_fee
                is_partial_refund = base_amount < remaining_amount

                # Determine if payment was disbursed before we change its status
                was_disbursed = payment.payable_type == 'order' and payment.status == 'DISBURSED'

                # Look up order for clawback before status change
                refund_order = None
                if was_disbursed:
                    refund_order = Order.objects.get(id=payment.payable_id)

                response = gateway.refund(transaction_id=payment.provider_reference, amount=refund_amount)
                if response and response.get('success'):
                    next_status = 'PARTIALLY_REFUNDED' if is_partial_refund else 'REFUNDED'
                    payment.status = PaymentFSM.transition(payment.status, next_status)
                    payment.refund_id = response.get('refund_id')
                    if not is_partial_refund:
                        payment.refunded_at = timezone.now()
                    payment.save()

                    # Escrow release
                    if was_disbursed:
                        _release_escrow_disbursed_refund(payment)
                    else:
                        _release_escrow_refund(base_amount, cancel_fee)

                    # Determine refund recipient
                    recipient = None
                    if payment.payable_type == 'order':
                        recipient = refund_order.customer if refund_order else Order.objects.get(id=payment.payable_id).customer
                    elif payment.payable_type == 'pool_member':
                        from apps.orders.models import PoolMember
                        recipient = PoolMember.objects.get(id=payment.payable_id).user

                    if recipient:
                        _create_double_entry(
                            payment=payment,
                            from_user=get_platform_user(),
                            to_user=recipient,
                            amount=refund_amount,
                            entry_type='platform_to_customer_refund',
                        )

                    if cancel_fee > 0:
                        _create_double_entry(
                            payment=payment,
                            from_user=recipient,
                            to_user=get_platform_user(),
                            amount=cancel_fee,
                            entry_type='platform_cancel_fee',
                        )

                    # Reverse section wallet credits if payment was previously disbursed
                    if was_disbursed and refund_order:
                        RefundPaymentService._clawback_section_wallets(payment, refund_order)

                    # Use a function to capture the current payment in the closure
                    def publish_refund(p=payment, amt=refund_amount):
                        EventBus.publish(PaymentRefunded(
                            aggregate_id=p.id,
                            payload={'type': p.payable_type, 'payable_id': str(p.payable_id), 'refund_amount': str(amt)}
                        ))
                    transaction.on_commit(publish_refund)
            
            return True

    @staticmethod
    def _clawback_section_wallets(payment, order):
        """
        Reverse the section wallet credits that were made when the payment was disbursed.
        Uses the same restaurant_payout formula as _distribute_restaurant_payout.
        
        NOTE: This only claws back section (stall) wallet credits.
        Restaurant owner and driver wallet credits are NOT clawed back —
        the platform absorbs that loss. A future enhancement could track
        negative balances or deduct from future payouts.
        """
        from apps.wallets.models import Wallet
        from domain.services.wallet_services import WalletDomainService

        items = list(order.items.select_related('food', 'section').all())
        if not items:
            return

        total_items_price = sum(item.price for item in items)
        if total_items_price == 0:
            return

        commission = order.restaurant.commission_rate if order.restaurant else Decimal('0.100')
        restaurant_payout = Decimal(str(order.items_subtotal or 0)) * (Decimal('1.0') - commission)

        distributed_amount = Decimal('0')
        cumulative_price = Decimal('0')
        for item in items:
            cumulative_price += Decimal(str(item.price))
            cumulative_target = (restaurant_payout * cumulative_price / total_items_price).quantize(Decimal('0.01'))
            item_payout = cumulative_target - distributed_amount
            distributed_amount += item_payout

            if item_payout <= 0 or not item.section:
                continue

            wallet, _ = Wallet.objects.get_or_create(section=item.section)
            try:
                WalletDomainService.debit_wallet(wallet, item_payout, f"Refund clawback for Order #{order.id}")
            except ValueError as e:
                if 'Insufficient' in str(e):
                    wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
                    actual_debit = min(wallet.balance, item_payout)
                    if actual_debit > 0:
                        WalletDomainService.debit_wallet(wallet, actual_debit,
                            f"Partial refund clawback for Order #{order.id}")
                    shortfall = item_payout - actual_debit
                    if shortfall > 0:
                        from apps.payments.models import WalletDebt
                        WalletDebt.objects.create(
                            payment=payment,
                            wallet=wallet,
                            original_amount=item_payout,
                            remaining_balance=shortfall,
                            status='OUTSTANDING',
                        )
                    logger.warning(
                        f"Section wallet {wallet.id} had only {wallet.balance} of {item_payout} "
                        f"for clawback; debited available balance."
                    )
                else:
                    raise
