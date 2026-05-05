from django.db import models
from apps.users.models import User
from django.contrib.gis.db import models as gis_models
from apps.core.geo_utils import latlng_to_h3
import uuid
from decimal import Decimal

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
        ('NEW', 'Brand New'),
        ('LIKE_NEW', 'Used - Like New'),
        ('GOOD', 'Used - Good'),
        ('FAIR', 'Used - Fair'),
    )

    LISTING_TYPE_CHOICES = (
        ('INITIAL', 'First Time Listing (2%)'),
        ('RENEWAL', 'Listing Renewal (1%)'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='marketplace_products')
    category = models.ForeignKey(MarketplaceCategory, on_delete=models.SET_NULL, null=True, related_name='products')
    
    name = models.CharField(max_length=255)
    description = models.TextField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='GOOD')
    
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
        percent = Decimal('2.00') if self.listing_type == 'INITIAL' else Decimal('1.00')
        return (self.price * percent / 100).quantize(Decimal('1.00'))

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.location:
            new_h3 = latlng_to_h3(self.location.y, self.location.x)
            if self.h3_index != new_h3:
                MarketplaceProduct.objects.filter(id=self.id).update(h3_index=new_h3)

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
