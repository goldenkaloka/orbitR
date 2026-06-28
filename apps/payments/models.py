import uuid

from apps.orders.models import Order
from apps.users.models import User
from django.db import models


class Payment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Polymorphic associations
    # Use payable_type and payable_id for unified access
    payable_type = models.CharField(max_length=50, db_index=True, null=True, blank=True)
    payable_id = models.CharField(max_length=255, db_index=True, null=True, blank=True)

    # Legacy associations (deprecated)
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='payment', null=True, blank=True)
    pool_member = models.OneToOneField('orders.PoolMember', on_delete=models.CASCADE, related_name='payment', null=True, blank=True)
    marketplace_product = models.ForeignKey('marketplace.MarketplaceProduct', on_delete=models.CASCADE, related_name='listing_payments', null=True, blank=True)

    
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=50, default='INITIATED', db_index=True)
    provider_reference = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    rrn = models.CharField(max_length=255, null=True, blank=True)
    refund_id = models.CharField(max_length=255, null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = [('payable_type', 'payable_id')]
        ordering = ['-created_at']

class Transaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='transactions')
    from_wallet = models.ForeignKey(User, on_delete=models.DO_NOTHING, related_name='outgoing_transactions', null=True, blank=True)
    to_wallet = models.ForeignKey(User, on_delete=models.DO_NOTHING, related_name='incoming_transactions', null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    transaction_type = models.CharField(max_length=50) # e.g. customer -> platform, platform -> restaurant
    status = models.CharField(max_length=50, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)

class FailedPayout(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='failed_payouts')
    to_wallet = models.ForeignKey(User, on_delete=models.DO_NOTHING, related_name='failed_payouts')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    transaction_type = models.CharField(max_length=50) # 'platform_to_restaurant' or 'platform_to_driver'
    reason = models.TextField(blank=True, null=True)
    error_code = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    retry_count = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)


class LedgerEntry(models.Model):
    """Proper double-entry accounting record referencing Wallet (not User)."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='ledger_entries')
    debit_wallet = models.ForeignKey('wallets.Wallet', on_delete=models.DO_NOTHING, related_name='debit_entries', null=True, blank=True)
    credit_wallet = models.ForeignKey('wallets.Wallet', on_delete=models.DO_NOTHING, related_name='credit_entries', null=True, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    entry_type = models.CharField(max_length=50)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class WalletDebt(models.Model):
    """Tracks unrecovered clawback amounts when a section wallet has insufficient balance."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='debts')
    wallet = models.ForeignKey('wallets.Wallet', on_delete=models.CASCADE, related_name='debts')
    original_amount = models.DecimalField(max_digits=10, decimal_places=2)
    remaining_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    recovered_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, default='OUTSTANDING')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)