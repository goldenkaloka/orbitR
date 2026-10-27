import datetime
import uuid
from decimal import Decimal

from apps.core.geo_utils import latlng_to_h3
from apps.users.models import User
from django.contrib.gis.db import models as gis_models
from django.db import models
from django.utils import timezone


class MarketplaceCategory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    icon = models.CharField(max_length=50, default='package') # Lucide icon name
    
    class Meta:
        verbose_name_plural = "Marketplace Categories"

    def __str__(self):
        return self.name

class MarketplaceProduct(models.Model):
    STATUS_CHOICES = (
        ('PENDING_PAYMENT', 'Pending Listing Fee'),
        ('ACTIVE', 'Active'),
        ('EXPIRED', 'Expired'),
        ('SOLD', 'Marked as Sold'),
        ('DELETED', 'Deleted'),
    )

    CONDITION_CHOICES = (
        ('NEW', 'New'),
        ('USED', 'Used'),
        ('OTHER', 'Other'),
    )

    LISTING_TYPE_CHOICES = (
        ('INITIAL', 'First Time Listing'),
        ('RENEWAL', 'Listing Renewal'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='marketplace_products')
    category = models.ForeignKey(MarketplaceCategory, on_delete=models.SET_NULL, null=True, related_name='products')
    
    name = models.CharField(max_length=255)
    description = models.TextField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='USED')
    
    # Contact
    whatsapp_number = models.CharField(max_length=20, help_text="WhatsApp phone number with country code")
    
    # Location & Zone
    location = gis_models.PointField(geography=True, null=True, blank=True, db_index=True)
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)
    
    # Lifecycle
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING_PAYMENT', db_index=True)
    listing_type = models.CharField(max_length=20, choices=LISTING_TYPE_CHOICES, default='INITIAL')
    expiry_date = models.DateTimeField(null=True, blank=True, db_index=True)
    
    # Fees
    is_fee_paid = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def calculate_listing_fee(self) -> Decimal:
        percent = Decimal('0.20')
        return (self.price * percent / 100).quantize(Decimal('1.00'))

    def save(self, *args, **kwargs):
        if self.location:
            new_h3 = latlng_to_h3(self.location.y, self.location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} - TSh {self.price}"

class ProductImage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product = models.ForeignKey(MarketplaceProduct, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='marketplace/products/')
    is_primary = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-is_primary', 'created_at']


class Shop(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='shops')
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    categories = models.ManyToManyField(MarketplaceCategory, blank=True, related_name='shops')
    location = gis_models.PointField(geography=True, null=True, blank=True, db_index=True)
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)
    whatsapp_number = models.CharField(max_length=20, blank=True, null=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.location:
            new_h3 = latlng_to_h3(self.location.y, self.location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.owner.username})"


class BuyerDemand(models.Model):
    STATUS_CHOICES = (
        ('OPEN', 'Open'),
        ('EXPIRED', 'Expired'),
        ('ACCEPTED', 'Accepted'),
        ('CANCELLED', 'Cancelled'),
    )

    RADIUS_CHOICES = (
        (1, '1 km'),
        (3, '3 km'),
        (5, '5 km'),
        (10, '10 km'),
        (20, '20 km'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    buyer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='demands')
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    location = gis_models.PointField(geography=True, null=True, blank=True, db_index=True)
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)
    radius_km = models.PositiveIntegerField(default=5, choices=RADIUS_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN', db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    accepted_offer = models.ForeignKey('SellerOffer', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    categories = models.ManyToManyField(MarketplaceCategory, blank=True, related_name='demands')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.location:
            new_h3 = latlng_to_h3(self.location.y, self.location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3
        if not self.expires_at and self.status == 'OPEN':
            self.expires_at = timezone.now() + datetime.timedelta(minutes=5)
        super().save(*args, **kwargs)

    def is_expired(self):
        return self.expires_at and timezone.now() >= self.expires_at

    def __str__(self):
        return f"{self.title} by {self.buyer.username} ({self.status})"


class SellerOffer(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending'),
        ('ACCEPTED', 'Accepted'),
        ('DECLINED', 'Declined'),
        ('WITHDRAWN', 'Withdrawn'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    demand = models.ForeignKey(BuyerDemand, on_delete=models.CASCADE, related_name='offers')
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='offers')
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='offers', null=True, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    availability = models.BooleanField(default=True, help_text="Does the seller have the item?")
    message = models.TextField(blank=True, null=True, help_text="Optional note from seller")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"TSh {self.price} by {self.seller.username} on {self.demand.title}"


class PostingTokenConfig(models.Model):
    free_tokens_for_new_users = models.PositiveIntegerField(default=5, help_text="Number of free tokens granted to new users on registration")
    token_deduction_per_listing = models.PositiveIntegerField(default=1, help_text="Number of tokens deducted per product listing")
    
    class Meta:
        verbose_name = "Posting Token Configuration"
        verbose_name_plural = "Posting Token Configurations"
        
    def __str__(self):
        return f"Tokens Config - Free: {self.free_tokens_for_new_users}, Cost: {self.token_deduction_per_listing}"


class PostingTokenBundleOption(models.Model):
    id = models.CharField(max_length=50, primary_key=True, help_text="Unique identifier (e.g., 'starter', 'pro', 'business')")
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    tokens_granted = models.PositiveIntegerField()
    is_active = models.BooleanField(default=True)
    
    class Meta:
        verbose_name = "Posting Token Bundle Option"
        verbose_name_plural = "Posting Token Bundle Options"
        
    def __str__(self):
        return f"{self.name} ({self.tokens_granted} tokens for TSh {self.price})"


class PostingTokenBundlePurchase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='token_bundle_purchases')
    bundle = models.ForeignKey(PostingTokenBundleOption, on_delete=models.CASCADE, related_name='purchases')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tokens_granted = models.PositiveIntegerField()
    is_paid = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - {self.bundle.name} - {'Paid' if self.is_paid else 'Pending'}"


class PostingTokenTransaction(models.Model):
    TRANSACTION_TYPE_CHOICES = (
        ('SIGNUP_BONUS', 'Signup Bonus'),
        ('PURCHASE', 'Bundle Purchase'),
        ('LISTING_DEDUCTION', 'Listing Fee Deduction'),
        ('LISTING_REFUND', 'Listing Fee Refund'),
        ('ADMIN_ADJUSTMENT', 'Admin Manual Adjustment'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='token_transactions')
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPE_CHOICES)
    amount = models.IntegerField(help_text="Token balance change amount (positive or negative)")
    balance_after = models.PositiveIntegerField(help_text="Token balance after this transaction completed")
    description = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.transaction_type} - {self.amount:+d} ({self.balance_after} left)"
