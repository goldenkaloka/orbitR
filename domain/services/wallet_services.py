from decimal import Decimal

from apps.wallets.models import Wallet, WalletTransaction
from django.db import IntegrityError, transaction

ZERO = Decimal('0.00')


class InsufficientFunds(ValueError):
    """Raised when a debit would take a wallet below zero."""


class WalletDomainService:
    """Core wallet movements.

    Every balance change goes through :meth:`post_transaction` so that the
    ledger row and the balance always move together, and replaying a call with
    the same ``reference`` or ``idempotency_key`` is a no-op.
    """

    @staticmethod
    def _get_locked_wallet(wallet=None, user=None):
        if wallet is not None:
            return Wallet.objects.select_for_update().get(pk=wallet.pk)
        if user is None:
            raise ValueError("A wallet or user is required.")
        try:
            with transaction.atomic():
                wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
                return wallet
        except IntegrityError:
            # A concurrent request created the wallet between our get and
            # create. Lock the winner instead of failing the whole movement.
            return Wallet.objects.select_for_update().get(user=user)

    @staticmethod
    def post_transaction(
        *,
        wallet=None,
        user=None,
        amount,
        tx_type,
        description,
        transaction_type='ADJUSTMENT',
        reference=None,
        idempotency_key=None,
        order=None,
        pool=None,
        payout=None,
        recover_debt=True,
    ):
        """Post one ledger movement.

        Returns ``(wallet, transaction, created)``. ``created`` is ``False``
        when an earlier call with the same reference/idempotency key already
        moved the money.
        """
        amount = Decimal(str(amount)).quantize(Decimal('0.01'))
        if amount <= 0:
            raise ValueError("Transaction amount must be greater than zero.")
        if tx_type not in ('CREDIT', 'DEBIT'):
            raise ValueError("tx_type must be CREDIT or DEBIT.")

        with transaction.atomic():
            wallet = WalletDomainService._get_locked_wallet(wallet=wallet, user=user)

            if idempotency_key:
                existing = WalletTransaction.objects.filter(
                    idempotency_key=idempotency_key,
                ).first()
                if existing:
                    return existing.wallet, existing, False

            if reference:
                existing = WalletTransaction.objects.filter(reference=reference).first()
                if existing:
                    if existing.wallet_id != wallet.id:
                        raise ValueError("This transaction reference is already in use.")
                    return existing.wallet, existing, False

            if tx_type == 'DEBIT':
                if wallet.balance < amount:
                    raise InsufficientFunds(
                        f"Insufficient wallet balance ({wallet.balance}) for debit of {amount}."
                    )
                balance_after = wallet.balance - amount
            else:
                balance_after = wallet.balance + amount

            txn = WalletTransaction.objects.create(
                wallet=wallet,
                amount=amount,
                tx_type=tx_type,
                transaction_type=transaction_type,
                description=description[:255],
                reference=reference,
                idempotency_key=idempotency_key,
                balance_after=balance_after,
                order=order,
                pool=pool,
                payout=payout,
            )

            wallet.balance = balance_after
            wallet.save(update_fields=['balance', 'updated_at'])

            if tx_type == 'CREDIT' and recover_debt and amount > 0:
                wallet = WalletDomainService._recover_debts(wallet, amount)

            return wallet, txn, True

    @staticmethod
    def _recover_debts(wallet, credit_amount):
        """Offset an outstanding clawback shortfall against new credit.

        Only the newly credited amount can be reclaimed; money already sitting
        in the wallet is never touched. Each recovery is its own ledger row so
        the restaurant's history shows exactly what was withheld.
        """
        from apps.payments.models import WalletDebt

        remaining_credit = credit_amount
        updated_wallet = wallet
        debts = (
            WalletDebt.objects.select_for_update()
            .filter(wallet=wallet, status='OUTSTANDING', remaining_balance__gt=0)
            .order_by('created_at')
        )
        for debt in debts:
            if remaining_credit <= 0:
                break
            applied = min(Decimal(str(debt.remaining_balance)), remaining_credit)
            if applied <= 0:
                continue

            new_recovered = Decimal(str(debt.recovered_amount)) + applied
            updated_wallet, _, _ = WalletDomainService.post_transaction(
                wallet=wallet,
                amount=applied,
                tx_type='DEBIT',
                transaction_type='DEBT_RECOVERY',
                description=f"Recovered outstanding balance for payment {debt.payment_id}",
                reference=f"debt:{debt.id}:recover:{int(new_recovered * 100)}",
                recover_debt=False,
            )

            debt.recovered_amount = new_recovered
            debt.remaining_balance = Decimal(str(debt.remaining_balance)) - applied
            if debt.remaining_balance <= 0:
                debt.status = 'SETTLED'
            debt.save(update_fields=['recovered_amount', 'remaining_balance', 'status', 'updated_at'])

            remaining_credit -= applied

        return updated_wallet

    @staticmethod
    def debit(user, amount: Decimal, description: str, **kwargs):
        return WalletDomainService.post_transaction(
            user=user, amount=amount, tx_type='DEBIT', description=description, **kwargs,
        )[0]

    @staticmethod
    def credit(user, amount: Decimal, description: str, **kwargs):
        return WalletDomainService.post_transaction(
            user=user, amount=amount, tx_type='CREDIT', description=description, **kwargs,
        )[0]

    @staticmethod
    def credit_wallet(wallet, amount: Decimal, description: str, **kwargs):
        return WalletDomainService.post_transaction(
            wallet=wallet, amount=amount, tx_type='CREDIT', description=description, **kwargs,
        )[0]

    @staticmethod
    def debit_wallet(wallet, amount: Decimal, description: str, **kwargs):
        return WalletDomainService.post_transaction(
            wallet=wallet, amount=amount, tx_type='DEBIT', description=description, **kwargs,
        )[0]
