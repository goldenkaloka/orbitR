from django.db import models
from apps.users.models import User
from apps.orders.models import Order
import uuid

# Note: In production/scaling with PostGIS, uncomment the following and change 'location' to a PointField.
# from django.contrib.gis.db import models as gis_models

from django.contrib.gis.db import models as gis_models
from apps.core.geo_utils import latlng_to_h3

class Driver(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='driver_profile')
    status = models.CharField(max_length=50, default='OFFLINE', db_index=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True, db_index=True)
    home_restaurant = models.ForeignKey(
        'restaurants.Restaurant', on_delete=models.CASCADE,
        related_name='drivers', null=True, blank=True,
        help_text='Home restaurant this driver is affiliated with'
    )
    serves_all = models.BooleanField(
        default=False,
        help_text='Platform driver — can serve orders from any restaurant'
    )
    can_assign = models.BooleanField(
        default=False,
        help_text='Can assign other drivers to batches'
    )
    managed_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL,
        null=True, blank=True,
        help_text='Supervisor driver who manages this driver'
    )
    priority = models.IntegerField(
        default=0,
        help_text='Higher priority = chosen first for dispatch, shown first in lists'
    )
    max_radius_km = models.DecimalField(
        max_digits=5, decimal_places=1, default=15.0,
        help_text='Maximum km from current location the driver will travel'
    )

    # PostGIS Location Field
    location = gis_models.PointField(geography=True, null=True, blank=True, db_index=True)
    # H3 Spatial Indexing
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)

    rating = models.DecimalField(max_digits=3, decimal_places=1, default=0.0)
    max_active_orders = models.PositiveIntegerField(default=10)
    current_active_orders = models.PositiveIntegerField(default=0)

    VEHICLE_CHOICES = (
        ('bicycle', 'Bicycle'),
        ('motorcycle', 'Motorcycle'),
        ('bajaj', 'Bajaj'),
        ('car', 'Car'),
    )
    vehicle_type = models.CharField(max_length=20, choices=VEHICLE_CHOICES, default='motorcycle')
    vehicle_number = models.CharField(max_length=50, blank=True, null=True)

    # Performance & Earnings
    total_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=0.0)
    today_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=0.0)

    def save(self, *args, **kwargs):
        if self.location:
            new_h3 = latlng_to_h3(self.location.y, self.location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3

        super().save(*args, **kwargs)

    def __str__(self):
        return f"Driver: {self.user.username}"

class DeliveryAssignment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='delivery_assignment')
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, related_name='assignments')
    assigned_at = models.DateTimeField(auto_now_add=True)
    picked_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

class DeliveryProof(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='delivery_proof')
    delivered_by = models.ForeignKey(Driver, on_delete=models.DO_NOTHING)
    photo_url = models.URLField(max_length=500, blank=True, null=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
