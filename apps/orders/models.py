from django.db import models
from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.products.models import Food
import uuid

from django.contrib.gis.db import models as gis_models
from apps.core.geo_utils import latlng_to_h3

class Order(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='orders', db_index=True)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='orders', db_index=True)
    pool = models.ForeignKey('OrderPool', on_delete=models.SET_NULL, null=True, blank=True, related_name='final_orders')
    
    # Batching & Zoning
    zone = models.ForeignKey('restaurants.DeliveryZone', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    batch = models.ForeignKey('DeliveryBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    
    status = models.CharField(max_length=50, default='PENDING_PAYMENT', db_index=True) 
    is_paid = models.BooleanField(default=False, db_index=True)
    is_settled = models.BooleanField(default=False, db_index=True)
    
    # Financials
    items_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=500.00)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    delivery_address = models.TextField(null=True, blank=True)
    
    # PostGIS Location Field
    delivery_location = gis_models.PointField(geography=True, null=True, blank=True, db_index=True)
    
    # H3 Spatial Indexing
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)
    
    notes = models.TextField(null=True, blank=True)
    driver = models.ForeignKey('delivery.Driver', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders', db_index=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    
    # OTP Delivery Verification
    delivery_otp = models.CharField(max_length=6, null=True, blank=True)
    
    # Group Ordering Metadata
    group_breakdown = models.JSONField(null=True, blank=True) # Stores {username, items, subtotal} for pool members
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    def save(self, *args, **kwargs):
        if self.delivery_location:
            new_h3 = latlng_to_h3(self.delivery_location.y, self.delivery_location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3

        super().save(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=['restaurant', 'status'], name='order_vendor_status_idx'),
            models.Index(fields=['driver', 'created_at'], name='order_driver_created_idx'),
        ]

    def __str__(self):
        return f"Order {self.id} - {self.status}"

class OrderItem(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    food = models.ForeignKey(Food, on_delete=models.SET_NULL, null=True, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    price = models.DecimalField(max_digits=10, decimal_places=2) # snapshot price at order time

    def __str__(self):
        return f"{self.quantity} x {self.food.name}"

class Dispute(models.Model):
    STATUS_CHOICES = (
        ('PENDING', 'Pending Review'),
        ('INVESTIGATING', 'Under Investigation'),
        ('RESOLVED', 'Resolved'),
        ('REJECTED', 'Rejected'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='disputes')
    raised_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='disputes_raised')
    
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    resolution = models.TextField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Dispute for Order {self.order.id} - {self.status}"

class OrderPool(models.Model):
    STATUS_CHOICES = (
        ('OPEN', 'Open for additions'),
        ('LOCKED', 'Locked for payment'),
        ('COMPLETED', 'Ordered'),
        ('CANCELLED', 'Cancelled'),
        ('EXPIRED', 'Expired'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    initiator = models.ForeignKey(User, on_delete=models.CASCADE, related_name='initiated_pools')
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='order_pools')
    
    invite_code = models.CharField(max_length=10, unique=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='OPEN', db_index=True)
    
    # Shared delivery details
    delivery_address = models.TextField()
    delivery_location = gis_models.PointField(geography=True, db_index=True)
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)
    
    total_delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    expires_at = models.DateTimeField()

    def save(self, *args, **kwargs):
        if self.delivery_location:
            new_h3 = latlng_to_h3(self.delivery_location.y, self.delivery_location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3
        super().save(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=['status', 'expires_at'], name='pool_status_expires_idx'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['initiator'], condition=models.Q(status='OPEN'), name='unique_open_pool_per_user')
        ]

    @property
    def is_expired(self):
        from django.utils import timezone
        return timezone.now() > self.expires_at

    def __str__(self):
        return f"Pool {self.invite_code} at {self.restaurant.name}"

class PoolMember(models.Model):
    STATUS_CHOICES = (
        ('JOINED', 'Joined'),
        ('READY', 'Ready to pay'),
        ('PAID', 'Paid share'),
    )

    pool = models.ForeignKey(OrderPool, on_delete=models.CASCADE, related_name='members')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='pool_memberships')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='JOINED')
    
    delivery_fee_share = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    items_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    joined_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('pool', 'user')

class PoolItem(models.Model):
    member = models.ForeignKey(PoolMember, on_delete=models.CASCADE, related_name='items')
    food = models.ForeignKey(Food, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    price_at_addition = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.quantity} x {self.food.name} (User: {self.member.user.username})"

class DeliveryBatch(models.Model):
    STATUS_CHOICES = (
        ('CREATED', 'Batch Created'),
        ('ASSIGNED', 'Rider Assigned'),
        ('PICKED', 'Rider Picked Up'),
        ('COMPLETED', 'All Delivered'),
        ('CANCELLED', 'Cancelled'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='batches')
    zone = models.ForeignKey('restaurants.DeliveryZone', on_delete=models.CASCADE, related_name='batches')
    driver = models.ForeignKey('delivery.Driver', on_delete=models.SET_NULL, null=True, blank=True, related_name='batches')
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='CREATED', db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    def __str__(self):
        return f"Batch {self.id} - {self.zone.name} ({self.status})"
