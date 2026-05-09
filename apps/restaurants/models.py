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
    
    # Legacy fields
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    
    # H3 Spatial Indexing
    h3_index = models.CharField(max_length=20, db_index=True, null=True, blank=True)

    category = models.CharField(max_length=100, default='General', db_index=True)
    price_range = models.CharField(max_length=50, default='TSH 2k–5k')
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=0.0)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    def save(self, *args, **kwargs):
        # Sync Point field with Decimal fields
        if self.geo_location:
            self.longitude = self.geo_location.x
            self.latitude = self.geo_location.y
        elif self.latitude and self.longitude:
            from django.contrib.gis.geos import Point
            self.geo_location = Point(float(self.longitude), float(self.latitude))
        
        super().save(*args, **kwargs)
        
        # After saving, ensure H3 index is synced if coordinates are present
        # We do this after super().save if we rely on DB defaults, but here we can do it before
        if self.latitude and self.longitude:
            new_h3 = latlng_to_h3(self.latitude, self.longitude)
            if self.h3_index != new_h3:
                # Update without triggering another save recursion
                Restaurant.objects.filter(id=self.id).update(h3_index=new_h3)

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

