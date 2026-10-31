import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP

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
from domain.services.payout_service import PayoutService
from infrastructure.payment_gateways.factory import get_payment_gateway

logger = logging.getLogger(__name__)


class PaymentTargetResolver:
    @staticmethod
    def resolve(payable_type, payable_id):
        if payable_type == 'order':
            order = Order.objects.get(id=payable_id)
            base_amount = order.total_amount
            phone = order.customer.phone
            user = order.customer
            desc = f"Payment for Order #{order.id}"
        elif payable_type == 'pool_member':
            from apps.orders.models import PoolMember
            member = PoolMember.objects.get(id=payable_id)
            base_amount = member.items_subtotal + member.delivery_fee_share + member.service_fee_share
            phone = member.user.phone
            user = member.user
            desc = f"Pool Member Share - {member.user.username}"
        elif payable_type == 'marketplace_product':
            from apps.marketplace.models import MarketplaceProduct
            product = MarketplaceProduct.objects.get(id=payable_id)
            base_amount = product.calculate_listing_fee()
            phone = product.seller.phone
            user = product.seller
            desc = f"Listing Fee for {product.name}"
        elif payable_type == 'posting_token_bundle':
            from apps.marketplace.models import PostingTokenBundlePurchase
            purchase = PostingTokenBundlePurchase.objects.get(id=payable_id)
            base_amount = purchase.amount
            phone = purchase.user.phone
            user = purchase.user
            desc = f"Purchase of {purchase.bundle.name} posting bundle"
        else:
            raise ValueError(f"Unknown payable_type: {payable_type}")

        # Charge exactly the resolved amount. The gateway's processing fee is
        # absorbed by the platform rather than silently marked up — the
        # customer receipt, the pay button and the actual collection must all
        # agree on one number.
        amount = Decimal(str(base_amount)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)

        return {
            'amount': amount,
            'phone': phone,
            'user': user,
            'description': desc
        }


def _can_pay_target(actor, target):
    if not actor or not actor.is_authenticated:
        return False
    if getattr(actor, 'is_staff', False) or getattr(actor, 'is_superuser', False):
        return True
    return target.get('user') == actor


def record_failed_payout(payment, reason):
    """
    Queue a failed ledger release for retry (DDIA Ch.11: dead-letter queue).

    ``retry_failed_payouts`` re-runs ReleasePaymentService for every queued
    row and deletes it on success. Without this, a single exception in the
    release path silently forfeits the restaurant/driver payout forever.
    """
    from apps.payments.models import FailedPayout

    to_user = None
    if payment.payable_type == 'order':
        order = Order.objects.filter(id=payment.payable_id).select_related(
            'restaurant__owner', 'driver__user'
        ).first()
        if order:
            if order.restaurant and order.restaurant.owner:
                to_user = order.restaurant.owner
            elif order.driver and order.driver.user:
                to_user = order.driver.user
    if to_user is None:
        to_user = get_platform_user()

    try:
        existing = FailedPayout.objects.filter(payment=payment).first()
        if existing:
            existing.reason = reason
            existing.save(update_fields=['reason'])
        else:
            FailedPayout.objects.create(
                payment=payment,
                to_wallet=to_user,
                amount=payment.amount,
                transaction_type='release_retry',
                reason=reason,
            )
        logger.warning(f"Queued failed payout for payment {payment.id}: {reason}")
    except Exception:
        logger.exception(f"Could not record FailedPayout for payment {payment.id}")


def record_failed_refund(payment, amount, reason):
    """Queue a refused refund so retry_failed_refunds can finish it."""
    from apps.payments.models import FailedRefund

    try:
        record, created = FailedRefund.objects.get_or_create(
            payment=payment,
            defaults={'amount': amount, 'reason': str(reason)[:2000]},
        )
        if not created:
            record.amount = amount
            record.reason = str(reason)[:2000]
            record.save(update_fields=['amount', 'reason'])
        logger.warning(f"Queued failed refund for payment {payment.id}: {reason}")
    except Exception:
        logger.exception(f"Could not record FailedRefund for payment {payment.id}")


def _refund_closed_order_payment(order_id):
    """Refund a collection that arrived after its order was closed."""
    try:
        RefundPaymentService.execute('order', order_id)
    except Exception:
        logger.exception(
            f"Could not refund late payment for closed order {order_id}",
        )


def _refund_closed_pool_member_payment(member_id):
    """Refund a member whose bucket was cancelled before their payment landed."""
    try:
        RefundPaymentService.execute('pool_member', member_id)
    except Exception:
        logger.exception(
            f"Could not refund late payment for closed pool member {member_id}",
        )


def _notify_preorder_booked(order):
    """Confirm a parked pre-order to the customer and the restaurant."""
    try:
        from domain.services.orders.scheduled_orders import format_eating_time
        from infrastructure.notifications.messages import (
            preorder_booked_customer,
            vendor_preorder_booked,
        )
        from infrastructure.notifications.service import NotificationService

        short_id = str(order.id)[:8]
        when_label = format_eating_time(order.scheduled_for)

        title, body = preorder_booked_customer(short_id, when_label)
        NotificationService.send_push(
            user_id=str(order.customer_id),
            title=title,
            body=body,
            data={
                'order_id': str(order.id),
                'type': 'preorder_booked',
            },
        )

        if order.restaurant and order.restaurant.owner_id:
            v_title, v_body = vendor_preorder_booked(short_id, when_label)
            NotificationService.send_push(
                user_id=str(order.restaurant.owner_id),
                title=v_title,
                body=v_body,
                data={
                    'order_id': str(order.id),
                    'type': 'vendor_preorder_booked',
                },
            )
    except Exception:
        logger.exception(f"Could not send pre-order booking notification for {order.id}")


class InitiatePaymentService:
    @staticmethod
    def execute(payable_type, payable_id, phone=None, bank_code=None, actor=None, method='MOBILE_MONEY'):
        if str(method or '').upper() == 'WALLET':
            return WalletCheckoutService.execute(
                payable_type=payable_type,
                payable_id=payable_id,
                actor=actor,
            )

        # Phase 1: resolve target and persist the Payment row. Short
        # transaction, no network I/O — a gateway stall must never hold a
        # Postgres row lock (DDIA Ch.1 "I/O in a transaction").
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

            reset_from_failed = False
            if not created and payment.status not in ['COLLECTED', 'DISBURSED']:
                if payment.amount != target['amount']:
                    payment.amount = target['amount']
                if payment.status == 'FAILED':
                    payment.status = 'INITIATED'
                    reset_from_failed = True
            payment.method = 'MOBILE_MONEY'

            # Sync legacy fields for backward compatibility
            if not payment.order and payable_type == 'order': payment.order_id = payable_id
            if not payment.pool_member and payable_type == 'pool_member': payment.pool_member_id = payable_id
            if not payment.marketplace_product and payable_type == 'marketplace_product': payment.marketplace_product_id = payable_id
            payment.save()

            # 3. Prevent double payment
            if payment.status in ['COLLECTED', 'DISBURSED']:
                raise ValueError("This has already been paid.")

            payment_id = payment.id

            # An INITIATED payment that already carries a gateway reference is a
            # push the customer has not answered yet. Reuse it rather than
            # firing a second real collect() (which would double-charge with a
            # fresh provider reference). A retry after FAILED is allowed.
            in_flight = (
                not created
                and not reset_from_failed
                and payment.status == 'INITIATED'
                and bool(payment.provider_reference)
            )

        # Reuse the in-flight push — no second network collection.
        if in_flight:
            logger.info(
                f"Reusing in-flight INITIATED payment {payment_id}; skipping duplicate collect"
            )
            return {
                'payment': payment,
                'gateway_response': {
                    'success': True,
                    'is_push': True,
                    'provider_reference': payment.provider_reference,
                },
                'is_push': True,
            }

        # 4. Initiate with Gateway — deliberately outside any transaction so
        # the 15-30s mobile-money response time holds neither a row lock nor
        # a checked-out connection.
        gateway = get_payment_gateway(phone=customer_phone)
        response = gateway.collect(
            amount=target['amount'],
            phone=customer_phone,
            order_reference=str(payment_id),  # Use Payment ID as gateway reference
            bank_code=bank_code
        )

        # 5. Record the outcome in a fresh short transaction. Re-lock so a
        # concurrent webhook that already completed this payment is not
        # clobbered.
        with transaction.atomic():
            payment = Payment.objects.select_for_update().get(id=payment_id)
            if payment.status in ['COLLECTED', 'DISBURSED']:
                return {
                    'payment': payment,
                    'gateway_response': response,
                    'is_push': response.get('success') and not response.get('instant_success')
                }
            if response.get('success'):
                payment.provider_reference = response.get('provider_reference') or response.get('transactionId')
                payment.save(update_fields=['provider_reference'])

                transaction.on_commit(lambda: EventBus.publish(PaymentInitiated(
                    aggregate_id=payment.id,
                    payload={
                        'payable_type': payable_type,
                        'payable_id': str(payable_id),
                        'amount': float(payment.amount),
                        'provider_reference': payment.provider_reference
                    }
                )))

        # --- AUTO-COMPLETE FOR MOCK --- runs outside the phase-5 transaction
        # because CompletePaymentService opens its own.
        if response.get('instant_success'):
            CompletePaymentService.execute(
                payment_id=payment.id,
                provider_reference=payment.provider_reference,
                success=True,
                result=response,
            )
            payment.refresh_from_db()

        return {
            'payment': payment,
            'gateway_response': response,
            'is_push': response.get('success') and not response.get('instant_success')
        }

class WalletCheckoutService:
    """Pay an existing payable from the customer's wallet balance.

    The debit and the payment completion share one transaction: if escrow,
    order confirmation or any downstream handler fails, the wallet is not
    charged. The ledger reference is tied to the payment id so a retried
    request can never move the same money twice.
    """

    @staticmethod
    def execute(payable_type, payable_id, actor=None):
        from domain.services.wallet_services import WalletDomainService

        if actor is None or not getattr(actor, 'is_authenticated', False):
            raise ValueError("Authentication is required to pay from a wallet.")

        try:
            payable_id = str(uuid.UUID(str(payable_id)))
        except (ValueError, AttributeError):
            pass

        with transaction.atomic():
            target = PaymentTargetResolver.resolve(payable_type, payable_id)
            if not _can_pay_target(actor, target):
                raise ValueError("You are not allowed to pay for this item.")
            if target['user'].id != actor.id:
                raise ValueError("Wallet payments can only be made by the account owner.")

            payment, created = Payment.objects.select_for_update().get_or_create(
                payable_type=payable_type,
                payable_id=payable_id,
                defaults={
                    'amount': target['amount'],
                    'status': 'INITIATED',
                    'method': 'WALLET',
                },
            )
            if not created and payment.status in [
                'COLLECTED', 'ON_HOLD', 'DISBURSED',
                'PARTIALLY_REFUNDED', 'REFUNDED',
            ]:
                raise ValueError("This has already been paid.")
            if (
                payment.status == 'INITIATED'
                and payment.method == 'MOBILE_MONEY'
                and payment.provider_reference
            ):
                raise ValueError(
                    "A mobile money request is already in progress for this payment. "
                    "Wait for it to finish before paying from your wallet."
                )

            payment.amount = target['amount']
            payment.method = 'WALLET'
            if payment.status == 'FAILED':
                payment.status = 'INITIATED'
            payment.provider_reference = f"WALLET_{payment.id.hex[:20]}"
            payment.save()

            WalletDomainService.post_transaction(
                user=target['user'],
                amount=target['amount'],
                tx_type='DEBIT',
                transaction_type='PAYMENT',
                description=f"Wallet payment — {target['description']}",
                reference=f"payment:{payment.id}:wallet-debit",
            )

            CompletePaymentService.execute(
                payment_id=payment.id,
                provider_reference=payment.provider_reference,
                success=True,
            )
            payment.refresh_from_db()

        return {
            'payment': payment,
            'gateway_response': {
                'success': True,
                'provider_reference': payment.provider_reference,
                'message': 'Paid from wallet',
                'instant_success': True,
            },
            'is_push': False,
            'instant_success': True,
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
    with transaction.atomic():
        wallet = _get_platform_wallet()
        wallet.pending_balance += amount
        wallet.save(update_fields=['pending_balance', 'updated_at'])


def order_gateway_fee(order) -> Decimal:
    """Gateway fee the platform already paid to settle this order at pickup.

    The restaurant was paid in full, but the gateway kept a fixed fee out of
    the platform's float. That fee is a platform cost, so it comes out of
    ``platform_cut`` — otherwise revenue is booked that never arrived.
    """
    if order is None or not getattr(order, 'pk', None):
        return Decimal('0')
    from apps.payments.models import PickupDisbursement
    fee = PickupDisbursement.objects.filter(
        order_id=order.pk,
        status=PickupDisbursement.STATUS_SUCCESS,
    ).values_list('gateway_fee', flat=True).first()
    return Decimal(str(fee or 0)).quantize(Decimal('0.01'))


def _release_escrow(total_amount: Decimal, platform_cut: Decimal, payment=None):
    """Move amount from escrow to cleared balance on disbursement.

    Escrow can drift (legacy rows, manual fixes, a pickup payout that already
    spent part of it). Never let that block a restaurant/driver payout: release
    what is actually held and let the platform absorb any shortfall.
    """
    from domain.services.wallet_services import WalletDomainService
    with transaction.atomic():
        wallet = _get_platform_wallet()
        total_amount = total_amount.quantize(Decimal('0.01'))
        released = min(wallet.pending_balance, total_amount)
        if released < total_amount:
            logger.error(
                f"[escrow] Short by {total_amount - released} when releasing "
                f"{total_amount} for payment {getattr(payment, 'id', None)}."
            )
        wallet.pending_balance -= released
        wallet.save(update_fields=['pending_balance', 'updated_at'])
        platform_cut = min(platform_cut.quantize(Decimal('0.01')), released)
        if platform_cut > 0:
            WalletDomainService.post_transaction(
                wallet=wallet,
                amount=platform_cut,
                tx_type='CREDIT',
                transaction_type='REVENUE',
                description='Platform commission on order payout',
                reference=f"revenue:{payment.id}:platform-cut" if payment else None,
                recover_debt=False,
            )


def debit_escrow_external(amount: Decimal, description: str = ''):
    """
    Take money out of escrow to pay someone outside the platform.

    Used by the pickup flow, where the platform sends a restaurant's share to
    their mobile money at the counter. Unlike _release_escrow nothing is
    retained as platform balance — the money leaves for good, so only
    pending_balance moves.

    Must be called with the order already locked so escrow and the disbursement
    record settle together.
    """
    amount = Decimal(amount).quantize(Decimal('0.01'))
    if amount <= 0:
        raise ValueError("Escrow debit amount must be greater than zero.")

    with transaction.atomic():
        wallet = _get_platform_wallet()
        if wallet.pending_balance < amount:
            raise ValueError(
                f"Insufficient escrow balance ({wallet.pending_balance}) "
                f"for payout of {amount}."
            )
        wallet.pending_balance -= amount
        wallet.save(update_fields=['pending_balance', 'updated_at'])
        logger.info(
            f"[escrow] Debited {amount} to external payee. {description}".strip()
        )
        return wallet.pending_balance


def restore_escrow_external(amount: Decimal, description: str = ''):
    """
    Put money back into escrow after an external payout failed.

    The inverse of debit_escrow_external — used so a failed restaurant payment
    can be retried rather than stranding the amount.
    """
    amount = Decimal(amount).quantize(Decimal('0.01'))
    if amount <= 0:
        raise ValueError("Escrow restore amount must be greater than zero.")

    with transaction.atomic():
        wallet = _get_platform_wallet()
        wallet.pending_balance += amount
        wallet.save(update_fields=['pending_balance', 'updated_at'])
        logger.info(
            f"[escrow] Restored {amount} to escrow. {description}".strip()
        )
        return wallet.pending_balance


def _record_platform_revenue(amount: Decimal, description: str, reference: str = None):
    """Record platform-owned money that is not held for a future payout."""
    from domain.services.wallet_services import WalletDomainService

    wallet = _get_platform_wallet()
    WalletDomainService.post_transaction(
        wallet=wallet,
        amount=amount,
        tx_type='CREDIT',
        transaction_type='REVENUE',
        description=description,
        reference=reference,
    )


def _release_escrow_refund(amount: Decimal, cancel_fee: Decimal, payment=None, fee_key=None):
    """On refund: release full amount from escrow, keep cancel fee as earned.

    Clamped to the escrow actually held so a refund never fails because an
    older order's escrow bookkeeping drifted.
    """
    from domain.services.wallet_services import WalletDomainService
    with transaction.atomic():
        wallet = _get_platform_wallet()
        amount = amount.quantize(Decimal('0.01'))
        released = min(wallet.pending_balance, amount)
        if released < amount:
            logger.error(
                f"[escrow] Short by {amount - released} when refunding "
                f"{amount} for payment {getattr(payment, 'id', None)}."
            )
        wallet.pending_balance -= released
        wallet.save(update_fields=['pending_balance', 'updated_at'])
        cancel_fee = min(cancel_fee.quantize(Decimal('0.01')), released)
        if cancel_fee > 0:
            WalletDomainService.post_transaction(
                wallet=wallet,
                amount=cancel_fee,
                tx_type='CREDIT',
                transaction_type='REVENUE',
                description='Cancellation fee',
                reference=f"revenue:{payment.id}:cancel-fee:{fee_key}" if payment else None,
                recover_debt=False,
            )


def _release_escrow_disbursed_refund(payment):
    """On refund of a disbursed payment: reverse the platform_cut from balance."""
    from domain.services.wallet_services import WalletDomainService
    from apps.payments.models import Transaction as PmtTransaction
    platform_entry = PmtTransaction.objects.filter(
        payment=payment,
        transaction_type='platform_revenue',
    ).first()
    if platform_entry and platform_entry.amount > 0:
        with transaction.atomic():
            wallet = _get_platform_wallet()
            reversible = min(wallet.balance, Decimal(str(platform_entry.amount)))
            if reversible > 0:
                WalletDomainService.post_transaction(
                    wallet=wallet,
                    amount=reversible,
                    tx_type='DEBIT',
                    transaction_type='REVERSAL',
                    description='Reversal of platform commission on refund',
                    reference=f"reversal:{payment.id}:platform-cut",
                    recover_debt=False,
                )
            if reversible < platform_entry.amount:
                logger.error(
                    f"[refund] Platform commission reversal short by "
                    f"{Decimal(str(platform_entry.amount)) - reversible} for payment {payment.id}"
                )


def _create_double_entry(payment, from_user, to_user, amount, entry_type, status='COMPLETED',
                         gateway_fee=Decimal('0')):
    """
    Create both a Transaction (User-based, backward compat) and a LedgerEntry (Wallet-based, double-entry).

    ``gateway_fee`` is only meaningful for ``platform_revenue`` rows: it is the
    payout cost already netted out of ``amount``, kept so gross and net cut can
    both be read from the ledger.
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
        gateway_fee=gateway_fee,
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
    def execute(payment_id=None, provider_reference=None, success=True, rrn=None, reference_id=None, result=None):
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
                
                # Only genuinely-finished states are terminal. FAILED is NOT
                # terminal: a late success callback (USSD push timed out but
                # the customer then approved it) must still be able to
                # collect the payment instead of being silently discarded.
                if payment.status in [
                    'COLLECTED', 'ON_HOLD', 'DISBURSED',
                    'PARTIALLY_REFUNDED', 'REFUNDED',
                ]:
                    logger.warning(
                        f"Payment {payment.id} already {payment.status} (likely webhook retry). "
                        f"Ignoring duplicate. provider_ref={provider_reference}"
                    )
                    return payment
                
                if success:
                    payment.status = PaymentFSM.transition(payment.status, 'COLLECTED')
                    payment.provider_reference = provider_reference or payment.provider_reference
                    payment.rrn = rrn

                    # Record what the gateway kept on the way in, when it says.
                    # There is no published collection schedule, so this is
                    # never estimated; release() subtracts it from revenue.
                    if result is not None:
                        from domain.services.gateway_fees import fee_from_gateway_result
                        collected_fee = fee_from_gateway_result(result)
                        if collected_fee is not None:
                            payment.gateway_fee = collected_fee.quantize(Decimal('0.01'))
                            payment.gateway_fee_source = 'GATEWAY'
                    
                    # DISPATCH TO HANDLER
                    if payment.payable_type == 'order':
                        CompletePaymentService._handle_order_payment(payment)
                    elif payment.payable_type == 'pool_member':
                        CompletePaymentService._handle_pool_payment(payment)
                    elif payment.payable_type == 'marketplace_product':
                        CompletePaymentService._handle_marketplace_payment(payment)
                    elif payment.payable_type == 'posting_token_bundle':
                        CompletePaymentService._handle_posting_token_bundle_payment(payment)
                    
                    payment.save()
                    
                    transaction.on_commit(lambda: EventBus.publish(PaymentCollected(
                        aggregate_id=payment.id,
                        payload={'payable_type': payment.payable_type, 'payable_id': str(payment.payable_id)}
                    )))
                else:
                    if payment.status != 'FAILED':
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
        from domain.services.orders.scheduled_orders import activate_paid_order

        order = Order.objects.select_for_update().select_related('restaurant').get(id=payment.payable_id)

        # A late success can arrive after cleanup_unpaid_orders cancelled the
        # order. The money is real, but the order is closed: record the
        # collection and refund it instead of trying CANCELLED -> CREATED
        # (which would roll the whole payment back and lose the collection).
        if order.status in ('CANCELLED', 'REFUNDED', 'EXPIRED', 'DELIVERED', 'RESOLVED'):
            _create_double_entry(
                payment=payment,
                from_user=order.customer,
                to_user=get_platform_user(),
                amount=payment.amount,
                entry_type='customer_to_platform',
            )
            _hold_escrow(payment.amount)
            _order_id = str(order.id)
            transaction.on_commit(
                lambda: _refund_closed_order_payment(_order_id),
            )
            return

        order.is_paid = True

        # A pre-order whose kitchen prep has not started yet is parked in
        # SCHEDULED; release_scheduled_orders moves it into the pipeline later.
        release_due = not (
            order.scheduled_for
            and order.release_at
            and order.release_at > timezone.now()
        )
        # Idempotent: a replayed webhook/event must not require a self-loop.
        if release_due:
            if order.status in ('PENDING_PAYMENT', 'SCHEDULED'):
                order.status = OrderFSM.transition(order.status, 'CREATED')
            elif order.status not in ('CREATED', 'AWAITING_CONFIRMATION'):
                order.status = OrderFSM.transition(order.status, 'CREATED')
        elif order.status == 'PENDING_PAYMENT':
            order.status = OrderFSM.transition(order.status, 'SCHEDULED')
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

        if order.status == 'SCHEDULED':
            # Tell the customer their booking is confirmed. Without this the
            # only message they get is at release time, hours or days later.
            transaction.on_commit(lambda: _notify_preorder_booked(order))
            return

        # Platform-managed/concierge listings are operated by real third-party
        # restaurants, so activation waits for ops confirmation before dispatch.
        activate_paid_order(order)

    @staticmethod
    def _handle_pool_payment(payment):
        from apps.orders.models import OrderPool, PoolMember
        member = PoolMember.objects.select_for_update().get(id=payment.payable_id)
        pool = OrderPool.objects.select_for_update().get(id=member.pool_id)

        # A member can pay just as the bucket is cancelled. Never resurrect a
        # cancelled bucket: record the collection and refund that member.
        if pool.status in ('CANCELLED', 'COMPLETED') or member.status == 'REFUNDED':
            _create_double_entry(
                payment=payment,
                from_user=member.user,
                to_user=get_platform_user(),
                amount=payment.amount,
                entry_type='pool_member_to_platform',
            )
            _hold_escrow(payment.amount)
            _member_id = str(member.id)
            transaction.on_commit(
                lambda: _refund_closed_pool_member_payment(_member_id),
            )
            return

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
        _record_platform_revenue(
            payment.amount,
            f"Marketplace listing fee for {product.id}",
            reference=f"revenue:{payment.id}:listing",
        )
        _create_double_entry(
            payment=payment,
            from_user=get_platform_user(),
            to_user=get_platform_user(),
            amount=payment.amount,
            entry_type='platform_revenue',
        )

    @staticmethod
    def _handle_posting_token_bundle_payment(payment):
        from apps.marketplace.models import PostingTokenBundlePurchase, PostingTokenTransaction
        purchase = PostingTokenBundlePurchase.objects.get(id=payment.payable_id)
        if purchase.is_paid:
            return
            
        with transaction.atomic():
            purchase.is_paid = True
            purchase.save(update_fields=['is_paid', 'updated_at'])
            
            user = purchase.user
            user.posting_tokens_balance += purchase.tokens_granted
            user.save(update_fields=['posting_tokens_balance'])
            
            # Record Token transaction log
            PostingTokenTransaction.objects.create(
                user=user,
                transaction_type='PURCHASE',
                amount=purchase.tokens_granted,
                balance_after=user.posting_tokens_balance,
                description=f"Purchased {purchase.bundle.name} bundle ({purchase.tokens_granted} tokens)"
            )
            
            # Double entry entries / platform revenue
            _create_double_entry(
                payment=payment,
                from_user=user,
                to_user=get_platform_user(),
                amount=payment.amount,
                entry_type='posting_bundle_purchase',
            )
            _record_platform_revenue(
                payment.amount,
                f"Posting token bundle purchase: {purchase.id}",
                reference=f"revenue:{payment.id}:tokens",
            )
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
                commission = order.restaurant.commission_rate if order.restaurant else Decimal('0.050')
                restaurant_payout = (Decimal(str(order.items_subtotal or 0)) * (Decimal('1.0') - commission)).quantize(Decimal('0.01'))

                driver_payout = PayoutService.order_share(order)

            elif payment.payable_type == 'pool_member':
                from apps.orders.models import OrderPool, PoolMember
                member = PoolMember.objects.select_related(
                    'pool', 'pool__restaurant', 'pool__restaurant__owner', 'pool__batch'
                ).get(id=payment.payable_id)
                OrderPool.objects.select_for_update().get(id=member.pool_id)
                commission = member.pool.restaurant.commission_rate if member.pool.restaurant else Decimal('0.050')
                restaurant_payout = (Decimal(str(member.items_subtotal or 0)) * (Decimal('1.0') - commission)).quantize(Decimal('0.01'))

                # Use this specific member's order (not just any pool order) so that
                # _distribute_restaurant_payout uses the right items for section credit.
                order = Order.objects.select_related(
                    'restaurant', 'restaurant__owner', 'driver', 'driver__user', 'batch'
                ).filter(pool=member.pool, customer=member.user).first()

                # Fallback: any order from the pool (e.g. if customer field differs)
                if not order:
                    order = Order.objects.select_related(
                        'restaurant', 'restaurant__owner', 'driver', 'driver__user', 'batch'
                    ).filter(pool=member.pool).order_by('-created_at').first()

                # Pool order drivers are set on the batch, not the order directly.
                # Resolve the driver from the batch if order.driver is None.
                if order and not order.driver and order.batch_id and order.batch.driver:
                    order.driver = order.batch.driver

                # The pool's own share of the fee is what was collected for this
                # drop when no trip was ever batched behind it.
                if order and order.batch_id and order.batch.driver_payout:
                    driver_payout = PayoutService.order_share(order)
                else:
                    driver_payout = Decimal(str(member.delivery_fee_share or 0)).quantize(Decimal('0.01'))

            if not order:
                raise ValueError("No payout target found for this payment.")

            refunded_principal = _refunded_principal_total(payment)
            net_collected_amount = payment.amount - refunded_principal
            if net_collected_amount < 0:
                raise ValueError("Refunded amount exceeds collected payment amount.")

            # The restaurant was already paid real money at the counter when the
            # driver collected the food. That amount has already left escrow, so
            # suppress the restaurant credit here and only release what is left.
            # Without this the restaurant is paid twice for one order.
            pickup_paid = Decimal(str(getattr(order, 'pickup_paid_amount', 0) or 0))
            if pickup_paid > 0:
                logger.info(
                    f"[ReleasePayment] Order {order.id} was paid {pickup_paid} to the "
                    f"restaurant at pickup — skipping delivery-time restaurant payout."
                )
                restaurant_payout = Decimal('0')

            escrow_release_amount = (net_collected_amount - pickup_paid).quantize(
                Decimal('0.01')
            )

            # Gateway fees are a real cost of moving the money. Subtract them
            # from the platform's share before it is booked as revenue: the
            # fee has already left the platform's float, so a cut that ignores
            # it is profit on paper only. Two fees apply — what was kept when
            # the customer's money came in, and what was kept when the
            # restaurant was paid.
            collection_fee = Decimal(str(payment.gateway_fee or 0)).quantize(Decimal('0.01'))
            gateway_fee = collection_fee + order_gateway_fee(order)

            platform_cut = (
                escrow_release_amount - restaurant_payout - driver_payout - gateway_fee
            ).quantize(Decimal('0.01'))
            if platform_cut < 0:
                # Driver minimum guarantee can exceed what a single order's slice of the
                # delivery/service fee covers.  The platform absorbs the shortfall rather
                # than blocking the payout.
                logger.warning(
                    f"[ReleasePayment] platform_cut negative ({platform_cut}) for payment "
                    f"{payment.id} — platform absorbing shortfall, capping cut to 0."
                )
                platform_cut = Decimal('0')

            _platform = get_platform_user()
            payment.status = PaymentFSM.transition(payment.status, 'DISBURSED')
            payment.save()

            # Release whatever escrow the pickup flow did not already spend.
            if escrow_release_amount > 0:
                _release_escrow(escrow_release_amount, platform_cut, payment=payment)

            # Credit wallets (ledger only — no real money moves)
            has_section_items = order.items.filter(section__isnull=False).exists() if order else False
            if restaurant_payout > 0 and order.restaurant:
                if has_section_items:
                    _distribute_restaurant_payout(order, restaurant_payout, payment=payment)
                elif order.restaurant.owner:
                    WalletDomainService.credit(
                        user=order.restaurant.owner,
                        amount=restaurant_payout,
                        description=f'Order payout — #{str(order.id)[:8]}',
                        transaction_type='EARNING',
                        reference=f"earning:{payment.id}:restaurant",
                    )

            if driver_payout > 0 and order.driver and order.driver.user:
                WalletDomainService.credit(
                    user=order.driver.user,
                    amount=driver_payout,
                    description=f'Delivery payout — #{str(order.id)[:8]}',
                    transaction_type='EARNING',
                    reference=f"earning:{payment.id}:driver",
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
                    gateway_fee=gateway_fee,
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


def _distribute_restaurant_payout(order, restaurant_payout, payment=None):
    """
    Distribute restaurant payout proportionally across sections.

    Routing logic per section:
    - If the section has dedicated SectionStaff → credit the section wallet
      (staff view their shared section wallet via the /wallets/me/ endpoint).
    - If the section has NO dedicated staff (e.g. auto-created "Main Kitchen"
      managed directly by the restaurant owner) → credit the owner's user wallet
      so the vendor can see the money in their personal wallet and cash out.

    Each credit carries a reference derived from the payment and item so a
    retried release cannot pay the same item twice.
    """
    from apps.wallets.models import Wallet
    from apps.restaurants.models import SectionStaff
    from domain.services.wallet_services import WalletDomainService

    items = list(order.items.select_related('food', 'section').all())
    if not items:
        return

    total_items_price = sum(item.price for item in items)
    if total_items_price == 0:
        return

    # Pre-fetch which section IDs have dedicated staff — those go to section wallets.
    # Sections without staff (incl. the auto-created "Main Kitchen") go to owner wallet.
    section_ids_with_items = {item.section_id for item in items if item.section_id}
    staffed_section_ids = set(
        SectionStaff.objects.filter(
            section_id__in=section_ids_with_items
        ).values_list('section_id', flat=True).distinct()
    )

    owner = order.restaurant.owner if order.restaurant else None

    distributed_amount = Decimal('0')
    cumulative_price = Decimal('0')
    for item in items:
        cumulative_price += Decimal(str(item.price))
        cumulative_target = (restaurant_payout * cumulative_price / total_items_price).quantize(Decimal('0.01'))
        item_payout = cumulative_target - distributed_amount
        distributed_amount += item_payout

        if item_payout <= 0 or not item.section:
            continue

        if item.section_id in staffed_section_ids:
            # Section has its own staff — credit the shared section wallet
            wallet, _ = Wallet.objects.get_or_create(section=item.section)
            WalletDomainService.credit_wallet(
                wallet,
                item_payout,
                f"Earnings for {item.food.name if item.food else 'item'} from Order #{order.id}",
                transaction_type='EARNING',
                reference=f"earning:{payment.id}:item:{item.id}" if payment else None,
            )
        else:
            # No dedicated staff — this is the owner-managed section (e.g. "Main Kitchen")
            # Credit the owner's personal user wallet so they can see it and cash out.
            if owner:
                WalletDomainService.credit(
                    user=owner,
                    amount=item_payout,
                    description=f"Earnings for {item.food.name if item.food else 'item'} from Order #{order.id}",
                    transaction_type='EARNING',
                    reference=f"earning:{payment.id}:item:{item.id}" if payment else None,
                )

class RefundPaymentService:
    @staticmethod
    def execute(
        payable_type,
        payable_id,
        override_amount: Decimal = None,
        cancel_fee_pct: Decimal = Decimal('0'),
        refund_to_wallet: bool = False,
    ):
        """
        Refund a collected payment.

        ``refund_to_wallet`` keeps the money on-platform by crediting the
        customer's wallet instead of calling the gateway. Wallet-funded
        payments always refund to the wallet — their money never left the
        platform, so a gateway refund would pay them twice.
        """
        from domain.services.wallet_services import WalletDomainService

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
            refunded_any = False

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

                recipient = None
                if payment.payable_type == 'order':
                    recipient = refund_order.customer if refund_order else Order.objects.get(id=payment.payable_id).customer
                elif payment.payable_type == 'pool_member':
                    from apps.orders.models import PoolMember
                    recipient = PoolMember.objects.get(id=payment.payable_id).user

                to_wallet = refund_to_wallet or payment.method == 'WALLET'
                if to_wallet and recipient is None:
                    raise ValueError("This payment has no wallet to refund to.")

                if to_wallet:
                    response = {
                        'success': True,
                        'refund_id': f"WALLET_{payment.id.hex[:16]}",
                    }
                else:
                    response = gateway.refund(
                        transaction_id=payment.provider_reference,
                        amount=refund_amount,
                    )

                if not (response and response.get('success')):
                    record_failed_refund(
                        payment,
                        refund_amount,
                        (response or {}).get('error')
                        or (response or {}).get('message')
                        or 'Gateway refused the refund',
                    )
                    continue

                refunded_any = True
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
                    _release_escrow_refund(
                        base_amount, cancel_fee, payment=payment,
                        fee_key=int(already_refunded * 100),
                    )

                if recipient:
                    if to_wallet:
                        WalletDomainService.post_transaction(
                            user=recipient,
                            amount=refund_amount,
                            tx_type='CREDIT',
                            transaction_type='REFUND',
                            description=f"Refund for {payable_type} {str(payable_id)[:8]}",
                            reference=f"refund:{payment.id}:{int(already_refunded * 100)}",
                        )
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
                    RefundPaymentService._clawback_section_wallets(
                        payment, refund_order, refund_key=int(already_refunded * 100),
                    )

                # Use a function to capture the current payment in the closure
                def publish_refund(p=payment, amt=refund_amount):
                    EventBus.publish(PaymentRefunded(
                        aggregate_id=p.id,
                        payload={'type': p.payable_type, 'payable_id': str(p.payable_id), 'refund_amount': str(amt)}
                    ))
                transaction.on_commit(publish_refund)

            return refunded_any

    @staticmethod
    def _clawback_section_wallets(payment, order, refund_key=0):
        """
        Reverse the section/owner wallet credits that were made when the payment was disbursed.
        Uses the same routing logic as _distribute_restaurant_payout (staffed → section wallet,
        unstaffed → owner user wallet).

        A shortfall (money already cashed out) is recorded as a WalletDebt instead
        of being silently dropped; future earnings offset it automatically.
        """
        from apps.wallets.models import Wallet
        from apps.restaurants.models import SectionStaff
        from domain.services.wallet_services import WalletDomainService

        items = list(order.items.select_related('food', 'section').all())
        if not items:
            return

        total_items_price = sum(item.price for item in items)
        if total_items_price == 0:
            return

        commission = order.restaurant.commission_rate if order.restaurant else Decimal('0.050')
        restaurant_payout = Decimal(str(order.items_subtotal or 0)) * (Decimal('1.0') - commission)

        section_ids_with_items = {item.section_id for item in items if item.section_id}
        staffed_section_ids = set(
            SectionStaff.objects.filter(
                section_id__in=section_ids_with_items
            ).values_list('section_id', flat=True).distinct()
        )

        owner = order.restaurant.owner if order.restaurant else None

        distributed_amount = Decimal('0')
        cumulative_price = Decimal('0')
        for item in items:
            cumulative_price += Decimal(str(item.price))
            cumulative_target = (restaurant_payout * cumulative_price / total_items_price).quantize(Decimal('0.01'))
            item_payout = cumulative_target - distributed_amount
            distributed_amount += item_payout

            if item_payout <= 0 or not item.section:
                continue

            if item.section_id in staffed_section_ids:
                wallet, _ = Wallet.objects.get_or_create(section=item.section)
            elif owner:
                wallet, _ = Wallet.objects.get_or_create(user=owner)
            else:
                wallet = None

            if wallet is not None:
                RefundPaymentService._debit_or_record_debt(
                    payment, wallet, item_payout, order, refund_key, item.id,
                )

    @staticmethod
    def _debit_or_record_debt(payment, wallet, amount, order, refund_key, item_id):
        from apps.payments.models import WalletDebt
        from apps.wallets.models import Wallet
        from domain.services.wallet_services import WalletDomainService

        wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        actual_debit = min(wallet.balance, amount)
        if actual_debit > 0:
            WalletDomainService.post_transaction(
                wallet=wallet,
                amount=actual_debit,
                tx_type='DEBIT',
                transaction_type='CLAWBACK',
                description=f"Refund clawback for Order #{order.id}",
                reference=f"clawback:{payment.id}:{refund_key}:{item_id}",
            )

        shortfall = amount - actual_debit
        if shortfall > 0:
            debt = WalletDebt.objects.select_for_update().filter(
                payment=payment, wallet=wallet, status='OUTSTANDING',
            ).first()
            if debt:
                debt.original_amount += amount
                debt.remaining_balance += shortfall
                debt.save(update_fields=['original_amount', 'remaining_balance', 'updated_at'])
            else:
                WalletDebt.objects.create(
                    payment=payment,
                    wallet=wallet,
                    original_amount=amount,
                    remaining_balance=shortfall,
                )
            logger.warning(
                f"[refund] Clawback shortfall {shortfall} for payment {payment.id} "
                f"wallet {wallet.id} recorded as WalletDebt."
            )
