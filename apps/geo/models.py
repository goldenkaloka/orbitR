from django.db import models
from django.contrib.gis.db import models as gis_models
import uuid

class OperationalZone(models.Model):
    """
    A generic geographic zone that can be used across the entire platform.
    Examples: "Main Campus", "Block B Hostels", "Science Faculty".
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    
    # PostGIS PolygonField stores the actual shape on the map
    boundary = gis_models.PolygonField(geography=True, db_index=True)
    
    is_active = models.BooleanField(default=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True) # For future expansion
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
