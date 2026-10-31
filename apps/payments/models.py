import uuid

from apps.orders.models import Order
from apps.users.models import User
from django.db import models

# Payment lifecycle vocabulary. Module-level because nested `class Meta`
# bodies cannot see class attributes.
PAYMENT_STATUS_CHOICES = (
    ('INITIATED', 'Initiated'),
    ('INPROCESS', 'In process'),
    ('COLLECTED', 'Collected'),
    ('ON_HOLD', 'On hold'),
    ('DISBURSED', 'Disbursed'),
    ('PARTIALLY_REFUNDED', 'Partially refunded'),
    ('REFUNDED', 'Refunded'),
    ('FAILED', 'Failed'),
)
PAYMENT_VALID_STATUSES = [c[0] for c in PAYMENT_STATUS_CHOICES]

PAYMENT_METHOD_MOBILE_MONEY = 'MOBILE_MONEY'
PAYMENT_METHOD_WALLET = 'WALLET'
PAYMENT_METHOD_CHOICES = (
    (PAYMENT_METHOD_MOBILE_MONEY, 'Mobile money'),
    (PAYMENT_METHOD_WALLET, 'Wallet'),
)


class Payment(models.Model):
    # Full lifecycle vocabulary enforced by PaymentFSM plus the legacy
    # INPROCESS marker still referenced by the stuck-payment cleanup task.
    STATUS_CHOICES = PAYMENT_STATUS_CHOICES
    VALID_STATUSES = PAYMENT_VALID_STATUSES

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
    method = models.CharField(
        max_length=20, choices=PAYMENT_METHOD_CHOICES,
        default=PAYMENT_METHOD_MOBILE_MONEY, db_index=True,
    )
    status = models.CharField(max_length=50, default='INITIATED', db_index=True, choices=STATUS_CHOICES)
    provider_reference = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    rrn = models.CharField(max_length=255, null=True, blank=True)
    # What the gateway kept when it collected this payment. Only populated when
    # the provider reports a fee; the platform has no published schedule for
    # collections, so this is never guessed. Subtracted from platform revenue
    # at release so the books match the money that actually arrived.
    gateway_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Gateway fee charged when collecting this payment (TZS)",
    )
    gateway_fee_source = models.CharField(
        max_length=16, blank=True, default='',
        help_text="GATEWAY when the provider reported the fee, ESTIMATE otherwise",
    )
    refund_id = models.CharField(max_length=255, null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = [('payable_type', 'payable_id')]
        ordering = ['-created_at']
        constraints = [
            # The FSM is advisory Python — this makes the database refuse any
            # status outside the lifecycle vocabulary (DDIA Ch.3: let storage
            # enforce invariants, not call-site convention).
            models.CheckConstraint(
                check=models.Q(status__in=PAYMENT_VALID_STATUSES),
                name='payment_status_valid',
            ),
        ]

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


class FailedRefund(models.Model):
    """A refund the gateway refused; retried by retry_failed_refunds.

    Without this queue a cancelled order could leave the customer paid: the
    refund service treats a refused gateway call as a no-op.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name='failed_refunds')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.TextField(blank=True, null=True)
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
    # Gateway cost already deducted from `amount` for platform_revenue rows.
    # Stored so gross cut and net cut can both be read off the ledger.
    gateway_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Payout fee the gateway charged, already netted out of amount",
    )
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


class PickupDisbursement(models.Model):
    """
    A real mobile-money payout to the restaurant at the moment of pickup.

    The platform pays the restaurant directly when the driver arrives, so the
    driver never carries cash for the food and the restaurant is settled before
    delivery rather than after.

    Unique per order on purpose. The gateways (ClickPesa in particular) append a
    random suffix to every payout reference to avoid duplicate-reference errors,
    which means a retry would happily create a *second real payout*. Idempotency
    therefore has to come from this constraint, not from the reference string:
    the row is claimed before the gateway is called, so a double-tap or a
    concurrent request loses the insert and never reaches the gateway.
    """

    STATUS_PENDING = 'PENDING'
    STATUS_SUCCESS = 'SUCCESS'
    STATUS_FAILED = 'FAILED'
    STATUS_REVERSED = 'REVERSED'
    STATUS_CHOICES = (
        (STATUS_PENDING, 'Pending'),
        (STATUS_SUCCESS, 'Paid'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_REVERSED, 'Reversed'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name='pickup_disbursement',
    )
    batch = models.ForeignKey(
        'orders.DeliveryBatch', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pickup_disbursements',
    )
    driver = models.ForeignKey(
        'delivery.Driver', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pickup_disbursements',
    )
    restaurant = models.ForeignKey(
        'restaurants.Restaurant', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pickup_disbursements',
    )

    amount = models.DecimalField(max_digits=10, decimal_places=2)
    # What the gateway charged the platform to make this transfer. The
    # restaurant still receives `amount`; the fee is a platform cost and is
    # deducted from platform_cut when the payment is released.
    gateway_fee = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Gateway fee for this payout (TZS)",
    )
    gateway_fee_source = models.CharField(
        max_length=16, blank=True, default='',
        help_text="GATEWAY when the provider reported the fee, ESTIMATE otherwise",
    )
    account_number = models.CharField(
        max_length=32,
        help_text="Restaurant Lipa Namba / phone the money was sent to",
    )
    network = models.CharField(
        max_length=32, blank=True, default='',
        help_text="Declared mobile-money network. Informational — the gateway "
                  "routes by MSISDN prefix, not by this value.",
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True,
    )
    gateway_reference = models.CharField(max_length=255, blank=True, default='')
    failure_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Pickup payment {self.order_id} — {self.amount} ({self.status})"