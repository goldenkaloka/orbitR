import uuid

from apps.orders.models import Order
from apps.users.models import User
from django.db import models

WALLET_TRANSACTION_TYPES = (
    ('PAYMENT', 'Payment'),
    ('REFUND', 'Refund'),
    ('PAYOUT', 'Payout'),
    ('FEE', 'Fee'),
    ('EARNING', 'Earning'),
    ('CLAWBACK', 'Clawback'),
    ('DEBT_RECOVERY', 'Debt recovery'),
    ('ADJUSTMENT', 'Adjustment'),
    ('REVENUE', 'Platform revenue'),
    ('REVERSAL', 'Reversal'),
)

WALLET_TRANSACTION_STATUSES = (
    ('POSTED', 'Posted'),
    ('REVERSED', 'Reversed'),
)

PAYOUT_STATUSES = (
    ('PENDING', 'Pending'),
    ('PROCESSING', 'Processing'),
    ('SUCCESS', 'Success'),
    ('FAILED', 'Failed'),
    ('UNKNOWN', 'Unknown'),
)

PAYOUT_VALID_STATUSES = [choice[0] for choice in PAYOUT_STATUSES]


class Wallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='wallet', null=True, blank=True)
    section = models.OneToOneField('restaurants.RestaurantSection', on_delete=models.CASCADE, related_name='wallet', null=True, blank=True)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    pending_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Balances are derived from the ledger. If a bug ever drives one
            # negative, fail the write instead of silently minting money.
            models.CheckConstraint(
                check=models.Q(balance__gte=0),
                name='wallet_balance_non_negative',
            ),
            models.CheckConstraint(
                check=models.Q(pending_balance__gte=0),
                name='wallet_pending_non_negative',
            ),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError
        if not self.user and not self.section:
            raise ValidationError("A wallet must belong to either a User or a RestaurantSection.")
        if self.user and self.section:
            raise ValidationError("A wallet cannot belong to both a User and a RestaurantSection.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        owner = self.user.username if self.user else f"Stall: {self.section.name}"
        return f"Wallet: {owner} ({self.balance})"


class WalletPayout(models.Model):
    """A real mobile-money payout requested from a wallet.

    The row is written before the gateway is called so a crash or a retried
    request cannot produce a second real payout: ``reference`` is unique and
    every wallet debit carries it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(
        Wallet, on_delete=models.CASCADE, related_name='payouts',
    )
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='wallet_payouts',
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2)
    provider = models.CharField(max_length=32, blank=True, default='')
    destination_phone = models.CharField(max_length=20)
    status = models.CharField(
        max_length=20, choices=PAYOUT_STATUSES, default='PENDING', db_index=True,
    )
    reference = models.CharField(max_length=64, unique=True)
    provider_reference = models.CharField(max_length=255, blank=True, default='')
    failure_reason = models.TextField(blank=True, default='')
    idempotency_key = models.CharField(
        max_length=128, unique=True, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                check=models.Q(status__in=PAYOUT_VALID_STATUSES),
                name='wallet_payout_status_valid',
            ),
        ]

    def __str__(self):
        return f"Payout {self.reference} — {self.net_amount} ({self.status})"


class WalletTransaction(models.Model):
    TRANSACTION_TYPES = (
        ('CREDIT', 'Credit'),
        ('DEBIT', 'Debit'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tx_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES, db_index=True)
    transaction_type = models.CharField(
        max_length=20, choices=WALLET_TRANSACTION_TYPES, default='ADJUSTMENT', db_index=True,
    )
    status = models.CharField(
        max_length=10, choices=WALLET_TRANSACTION_STATUSES, default='POSTED', db_index=True,
    )
    description = models.CharField(max_length=255)
    # Business reference for the movement (for example ``payout:<id>:wallet:<id>``).
    # Unique so replaying a service call cannot move the same money twice.
    reference = models.CharField(max_length=96, unique=True, null=True, blank=True)
    idempotency_key = models.CharField(
        max_length=128, unique=True, null=True, blank=True,
    )
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True)
    pool = models.ForeignKey('orders.OrderPool', on_delete=models.SET_NULL, null=True, blank=True)
    payout = models.ForeignKey(
        WalletPayout, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='transactions',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['wallet', '-created_at'], name='wallet_txn_recent_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(amount__gt=0),
                name='wallet_transaction_amount_positive',
            ),
        ]

    def __str__(self):
        owner = self.wallet.user.username if self.wallet.user else f"Stall: {self.wallet.section.name}"
        return f"{self.tx_type} | {self.amount} | {owner}"
