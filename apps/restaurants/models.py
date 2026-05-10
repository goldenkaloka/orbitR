from django.db import models
from apps.users.models import User
import uuid

from django.contrib.gis.db import models as gis_models
from apps.core.geo_utils import latlng_to_h3

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

class DeliveryZone(models.Model):
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='delivery_zones')
    name = models.CharField(max_length=100)
    
    # H3 cell index that represents this zone
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)
    
    fee = models.DecimalField(max_digits=10, decimal_places=2, default=500.00)
    priority = models.IntegerField(default=0, help_text="Higher priority zones take precedence if overlapping")
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # We allow h3_index to be null initially if the field definition says so,
        # but we encourage it to be set. The contradiction is removed here.
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.restaurant.name} - {self.name} ({self.fee} TZS)"

