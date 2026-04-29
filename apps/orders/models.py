from django.db import models
from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.products.models import Food
import uuid

from django.contrib.gis.db import models as gis_models

class Order(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='orders', db_index=True)
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='orders', db_index=True)
    
    status = models.CharField(max_length=50, default='CREATED', db_index=True) 
    is_settled = models.BooleanField(default=False, db_index=True)
    
    # Financials
    items_subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=500.00)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    delivery_address = models.TextField(null=True, blank=True)
    
    # PostGIS Location Field
    delivery_location = gis_models.PointField(geography=True, null=True, blank=True, db_index=True)
    
    # Legacy fields
    delivery_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    delivery_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    
    notes = models.TextField(null=True, blank=True)
    driver = models.ForeignKey('delivery.Driver', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders', db_index=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    
    # OTP Delivery Verification
    delivery_otp = models.CharField(max_length=6, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

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
    food = models.ForeignKey(Food, on_delete=models.DO_NOTHING)
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
