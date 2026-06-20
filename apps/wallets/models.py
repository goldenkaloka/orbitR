import uuid

from apps.orders.models import Order
from apps.users.models import User
from django.db import models


class Wallet(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='wallet', null=True, blank=True)
    section = models.OneToOneField('restaurants.RestaurantSection', on_delete=models.CASCADE, related_name='wallet', null=True, blank=True)
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    pending_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    updated_at = models.DateTimeField(auto_now=True)

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

class WalletTransaction(models.Model):
    TRANSACTION_TYPES = (
        ('CREDIT', 'Credit'),
        ('DEBIT', 'Debit'),
    )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tx_type = models.CharField(max_length=10, choices=TRANSACTION_TYPES, db_index=True)
    description = models.CharField(max_length=255)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True)
    pool = models.ForeignKey('orders.OrderPool', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        owner = self.wallet.user.username if self.wallet.user else f"Stall: {self.wallet.section.name}"
        return f"{self.tx_type} | {self.amount} | {owner}"
