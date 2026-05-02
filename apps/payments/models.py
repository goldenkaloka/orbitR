from django.db import models
from apps.orders.models import Order
from apps.users.models import User
import uuid

class Payment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Polymorphic-like associations
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='payment', null=True, blank=True)
    marketplace_product = models.ForeignKey('marketplace.MarketplaceProduct', on_delete=models.CASCADE, related_name='listing_payments', null=True, blank=True)
    
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=50, default='INITIATED', db_index=True)
    provider_reference = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    rrn = models.CharField(max_length=255, null=True, blank=True)
    refund_id = models.CharField(max_length=255, null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

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