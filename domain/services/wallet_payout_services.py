import logging
import uuid
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from apps.wallets.models import Wallet, WalletPayout
from django.db import IntegrityError, transaction
from django.utils import timezone
from domain.services.wallet_services import InsufficientFunds, WalletDomainService
from infrastructure.payment_gateways.factory import get_payment_gateway

logger = logging.getLogger(__name__)

INSTANT_FEE_PERCENT = Decimal('0.005')  # 0.5%
MINIMUM_CASHOUT = Decimal('1000')
MAXIMUM_CASHOUT = Decimal('10000000')
CASHOUT_ROLES = {'driver', 'vendor', 'vendor_staff', 'admin'}

WHOLE_TZS = Decimal('1')


class CashoutError(Exception):
    def __init__(self, code, message, status_code=400, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra


def cashout_wallets(user):
    """Wallets this user may cash out, in debit order.

    - Stall staff draw from their section wallet only.
    - Restaurant owners draw from their personal wallet first, then every
      section wallet of the restaurant (this is the same total ``/wallets/me``
      shows them).
    - Drivers and admins draw from their personal wallet.
    """
    from apps.restaurants.models import Restaurant, SectionStaff

    staff_link = SectionStaff.objects.filter(user=user).select_related('section').first()
    if staff_link:
        wallet, _ = Wallet.objects.get_or_create(section=staff_link.section)
        return [wallet]

    wallet, _ = Wallet.objects.get_or_create(user=user)
    wallets = [wallet]

    if getattr(user, 'role', None) == 'vendor':
        restaurant = Restaurant.objects.filter(owner=user).first()
        if restaurant:
            section_wallets = list(
                Wallet.objects.filter(section__restaurant=restaurant).order_by('id')
            )
            wallets.extend(section_wallets)

    # Deduplicate while preserving order (a section wallet cannot be the user
    # wallet, but this guards against data anomalies).
    seen = set()
    unique = []
    for w in wallets:
        if w.id in seen:
            continue
        seen.add(w.id)
        unique.append(w)
    return unique


def wallet_summary(user):
    """Aggregate balance/pending across every wallet the user can spend from."""
    wallets = cashout_wallets(user)
    balance = sum((w.balance for w in wallets), Decimal('0'))
    pending = sum((w.pending_balance for w in wallets), Decimal('0'))
    return wallets, balance, pending


class CashoutService:
    @staticmethod
    def execute(user, amount=None, phone='', provider='M_PESA', idempotency_key=None):
        role = getattr(user, 'serialized_role', getattr(user, 'role', 'customer'))
        if role not in CASHOUT_ROLES:
            raise CashoutError(
                'CASHOUT_NOT_ALLOWED',
                'Wallet balances can be used to pay for orders, but cannot be cashed out. '
                'Only drivers, restaurant owners and stall staff can withdraw earnings.',
                status_code=403,
            )

        wallets = cashout_wallets(user)
        if not wallets:
            raise CashoutError('NO_WALLET', 'No wallet found for this account.', status_code=400)

        if idempotency_key:
            existing = WalletPayout.objects.filter(
                idempotency_key=idempotency_key,
            ).first()
            if existing:
                if existing.actor_id != user.id:
                    raise CashoutError(
                        'IDEMPOTENCY_KEY_CONFLICT',
                        'This request id was already used by another account. '
                        'Please retry with a new request.',
                        status_code=409,
                    )
                return existing

        with transaction.atomic():
            # Lock every wallet in a stable order so concurrent cashouts and
            # payouts cannot deadlock each other.
            locked = {
                w.id: w for w in Wallet.objects.select_for_update()
                .filter(id__in=[w.id for w in wallets])
                .order_by('id')
            }
            wallets = [locked[w.id] for w in wallets if w.id in locked]
            available = sum((w.balance for w in wallets), Decimal('0'))

            if idempotency_key:
                existing = WalletPayout.objects.select_for_update().filter(
                    idempotency_key=idempotency_key,
                ).first()
                if existing:
                    if existing.actor_id != user.id:
                        raise CashoutError(
                            'IDEMPOTENCY_KEY_CONFLICT',
                            'This request id was already used by another account. '
                            'Please retry with a new request.',
                            status_code=409,
                        )
                    return existing

            # A client that does not send an idempotency key must not be able
            # to double-withdraw by double-tapping the button.
            cutoff = timezone.now() - timedelta(minutes=2)
            if WalletPayout.objects.filter(
                actor=user,
                status__in=['PENDING', 'PROCESSING'],
                created_at__gte=cutoff,
            ).exists():
                raise CashoutError(
                    'PAYOUT_IN_PROGRESS',
                    'A cashout is already being processed. Please wait a moment.',
                    status_code=409,
                )

            requested = available if amount is None else Decimal(str(amount))
            requested = requested.quantize(WHOLE_TZS, rounding=ROUND_HALF_UP)

            if requested <= 0:
                raise CashoutError('AMOUNT_TOO_LOW', 'Amount must be greater than zero.')
            if requested < MINIMUM_CASHOUT:
                raise CashoutError(
                    'AMOUNT_BELOW_MINIMUM',
                    f'Minimum cashout amount is TSh {int(MINIMUM_CASHOUT)}.',
                    minimum_required=float(MINIMUM_CASHOUT),
                )
            if requested > MAXIMUM_CASHOUT:
                raise CashoutError(
                    'AMOUNT_TOO_HIGH',
                    f'Maximum cashout amount is TSh {int(MAXIMUM_CASHOUT)}.',
                )
            if requested > available:
                raise CashoutError(
                    'INSUFFICIENT_BALANCE',
                    'Amount exceeds your available balance.',
                    available_balance=float(available),
                )

            fee = (requested * INSTANT_FEE_PERCENT).quantize(WHOLE_TZS, rounding=ROUND_HALF_UP)
            net_amount = requested - fee
            if net_amount <= 0:
                raise CashoutError('FEES_EXCEED_AMOUNT', 'Balance too low to cover fees.')

            try:
                with transaction.atomic():
                    payout = WalletPayout.objects.create(
                        wallet=wallets[0],
                        actor=user,
                        amount=requested,
                        fee=fee,
                        net_amount=net_amount,
                        provider=provider,
                        destination_phone=phone,
                        status='PENDING',
                        reference=f"cashout_{uuid.uuid4().hex[:24]}",
                        idempotency_key=idempotency_key,
                    )
            except IntegrityError:
                # Two requests arrived with the same Idempotency-Key; return
                # the winner rather than charging twice.
                if idempotency_key:
                    winner = WalletPayout.objects.filter(
                        idempotency_key=idempotency_key, actor=user,
                    ).first()
                    if winner:
                        return winner
                raise CashoutError(
                    'IDEMPOTENCY_KEY_CONFLICT',
                    'This request id was already used. Please retry with a new request.',
                    status_code=409,
                )
            remaining = requested
            for wallet in wallets:
                if remaining <= 0:
                    break
                take = min(wallet.balance, remaining)
                if take <= 0:
                    continue
                WalletDomainService.post_transaction(
                    wallet=wallet,
                    amount=take,
                    tx_type='DEBIT',
                    transaction_type='PAYOUT',
                    description=f'Cashout to {provider}',
                    reference=f"payout:{payout.id}:wallet:{wallet.id}",
                    payout=payout,
                    recover_debt=False,
                )
                remaining -= take

            if remaining > 0:
                # Balances moved under us despite the locks; abort cleanly.
                raise CashoutError(
                    'INSUFFICIENT_BALANCE',
                    'Amount exceeds your available balance.',
                    status_code=409,
                    available_balance=float(available - (requested - remaining)),
                )

            if fee > 0:
                from domain.services.payment_services import _get_platform_wallet
                platform_wallet = _get_platform_wallet()
                WalletDomainService.post_transaction(
                    wallet=platform_wallet,
                    amount=fee,
                    tx_type='CREDIT',
                    transaction_type='FEE',
                    description=f'Cashout fee — payout {payout.id}',
                    reference=f"payout:{payout.id}:fee",
                    payout=payout,
                    recover_debt=False,
                )

            payout.status = 'PROCESSING'
            payout.save(update_fields=['status', 'updated_at'])

        # Gateway call happens after the debit is committed. The payout row is
        # the source of truth: a crash between here and the response leaves it
        # PROCESSING and the reconciliation task settles it.
        gateway = get_payment_gateway(phone=phone)
        try:
            result = gateway.disburse(
                amount=net_amount,
                account_info=phone,
                reference=payout.reference,
                narration=f'Wallet cashout to {provider}',
            )
        except Exception as exc:
            logger.exception(f"Cashout gateway exception for payout {payout.id}")
            result = {'success': False, 'unknown': True, 'error': str(exc)}

        if result.get('success'):
            payout.status = 'SUCCESS'
            payout.provider_reference = result.get('provider_reference') or ''
            payout.completed_at = timezone.now()
            payout.failure_reason = ''
            payout.save(update_fields=[
                'status', 'provider_reference', 'completed_at', 'failure_reason', 'updated_at',
            ])
            logger.info(
                f"Cashout {payout.id} succeeded: amount={payout.amount} net={payout.net_amount} "
                f"provider_ref={payout.provider_reference}"
            )
            return payout

        reason = str(result.get('error') or 'Disbursement failed')
        if result.get('unknown'):
            # The gateway may have paid out. Do NOT reverse: the reconciliation
            # task polls the provider and only reverses on a definitive failure.
            payout.status = 'UNKNOWN'
            payout.failure_reason = reason
            payout.save(update_fields=['status', 'failure_reason', 'updated_at'])
            logger.error(f"Cashout {payout.id} outcome unknown: {reason}")
            return payout

        logger.error(f"Cashout {payout.id} rejected by gateway: {reason}")
        CashoutService._reverse_payout(payout, reason)
        return payout

    @staticmethod
    def _reverse_payout(payout, reason):
        """Return the debited money to the source wallets after a failed payout."""
        try:
            with transaction.atomic():
                payout = WalletPayout.objects.select_for_update().get(id=payout.id)
                if payout.status in ('FAILED', 'SUCCESS'):
                    return payout

                for txn in payout.transactions.select_related('wallet').order_by('created_at'):
                    reverse_type = 'CREDIT' if txn.tx_type == 'DEBIT' else 'DEBIT'
                    try:
                        WalletDomainService.post_transaction(
                            wallet=txn.wallet,
                            amount=txn.amount,
                            tx_type=reverse_type,
                            transaction_type='REVERSAL',
                            description=f'Cashout reversal — payout {payout.id}',
                            reference=f"payout:{payout.id}:reversal:{txn.id}",
                            payout=payout,
                            recover_debt=False,
                        )
                    except InsufficientFunds:
                        # Only the platform fee leg can hit this (the customer
                        # debits are being credited back). Refund the customer
                        # anyway and flag the fee leg for ops instead of
                        # aborting the whole reversal.
                        logger.error(
                            f"Could not reverse fee leg {txn.id} for payout {payout.id}"
                        )

                payout.status = 'FAILED'
                payout.failure_reason = reason
                payout.completed_at = timezone.now()
                payout.save(update_fields=['status', 'failure_reason', 'completed_at', 'updated_at'])
                return payout
        except Exception as exc:
            logger.exception(f"Could not reverse cashout {payout.id}")
            WalletPayout.objects.filter(id=payout.id).update(
                status='UNKNOWN',
                failure_reason=f'Reversal failed: {exc}',
                updated_at=timezone.now(),
            )
            payout.refresh_from_db()
            return payout


def reconcile_payout(payout_id, gateway=None):
    """Ask the provider what happened to a payout whose outcome is unknown."""
    payout = WalletPayout.objects.get(id=payout_id)
    if payout.status not in ('PROCESSING', 'UNKNOWN'):
        return payout
    if not payout.provider_reference:
        # Without a provider reference we cannot query the provider. Leave the
        # payout for manual ops review rather than guessing and reversing.
        logger.warning(f"Payout {payout.id} has no provider reference; manual review required.")
        return payout

    gateway = gateway or get_payment_gateway()
    result = gateway.get_status(provider_reference=payout.provider_reference)
    if not result.get('success'):
        logger.warning(f"Payout {payout.id} status lookup failed: {result.get('error')}")
        return payout

    status = result.get('status')
    if status == 'SUCCESS':
        payout.status = 'SUCCESS'
        payout.completed_at = timezone.now()
        payout.failure_reason = ''
        payout.save(update_fields=['status', 'completed_at', 'failure_reason', 'updated_at'])
    elif status == 'FAILED':
        CashoutService._reverse_payout(payout, 'Provider reported the payout as failed.')
    return payout
