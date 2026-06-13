from django.db import models
from apps.users.models import User
import uuid
from decimal import Decimal

from django.contrib.gis.db import models as gis_models
from apps.core.geo_utils import latlng_to_h3


class RestaurantSection(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    restaurant = models.ForeignKey('Restaurant', on_delete=models.CASCADE, related_name='sections')
    name = models.CharField(max_length=255)
    specialization = models.CharField(max_length=100, blank=True, db_index=True)
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='sections/', blank=True, null=True)
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.PositiveIntegerField(default=0)
    prep_time = models.PositiveIntegerField(default=20, help_text='Preparation time in minutes')

    class Meta:
        ordering = ['sort_order', 'name']

    def __str__(self):
        return f"{self.name} @ {self.restaurant.name}"


class SectionStaff(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='managed_stalls')
    section = models.ForeignKey(RestaurantSection, on_delete=models.CASCADE, related_name='staff')

    class Meta:
        unique_together = ('user', 'section')

    def __str__(self):
        return f"{self.user.username} → {self.section.name}"


class Restaurant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='restaurants', db_index=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='restaurants/', blank=True, null=True)
    location = models.CharField(max_length=255) # Text address
    
    # PostGIS Location Field for the restaurant itself
    geo_location = gis_models.PointField(geography=True, null=True, blank=True, db_index=True)
    
    # H3 Spatial Indexing
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)

    # Maximum delivery radius — replaces DeliveryZone model entirely.
    # "Does this restaurant deliver here?" = distance <= max_delivery_radius_km
    max_delivery_radius_km = models.FloatField(
        default=5.0,
        help_text="Maximum delivery distance in km from the restaurant"
    )

    # Average food preparation time in minutes — used for ETA calculation
    average_prep_time = models.PositiveIntegerField(
        default=20,
        help_text="Average preparation time in minutes"
    )

    # Commission rate for platform monetization
    commission_rate = models.DecimalField(
        max_digits=4, 
        decimal_places=3, 
        default=Decimal('0.100'),
        help_text="Percentage of food subtotal taken by the platform (e.g., 0.100 for 10%)"
    )

    category = models.CharField(max_length=100, default='General', db_index=True)
    price_range = models.CharField(max_length=50, default='TSH 2k–5k')
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=0.0)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    def save(self, *args, **kwargs):
        if self.geo_location:
            new_h3 = latlng_to_h3(self.geo_location.y, self.geo_location.x)
            if self.h3_index != new_h3:
                self.h3_index = new_h3
        super().save(*args, **kwargs)


    def __str__(self):
        return self.name
