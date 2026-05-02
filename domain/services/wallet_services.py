from apps.wallets.models import Wallet, WalletTransaction
from django.db import transaction
from decimal import Decimal

class WalletDomainService:
    """
    Core domain logic for wallet movements.
    Ensures atomic updates and detailed transaction logging.
    """
    
    @staticmethod
    def debit(user, amount: Decimal, description: str):
        with transaction.atomic():
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
            if wallet.balance < amount:
                raise ValueError(f"Insufficient funds in wallet for {user.username}")
            
            wallet.balance -= amount
            wallet.save()
            
            WalletTransaction.objects.create(
                wallet=wallet,
                amount=amount,
                tx_type='DEBIT',
                description=description
            )
            return wallet

    @staticmethod
    def credit(user, amount: Decimal, description: str):
        with transaction.atomic():
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
            wallet.balance += amount
            wallet.save()
            
            WalletTransaction.objects.create(
                wallet=wallet,
                amount=amount,
                tx_type='CREDIT',
                description=description
            )
            return wallet
            
    @staticmethod
    def move_escrow_to_pending(user, amount: Decimal):
        """Used when a payment is collected but not yet released."""
        with transaction.atomic():
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
            wallet.pending_balance += amount
            wallet.save()
            return wallet
