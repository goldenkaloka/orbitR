from django.db import models
from django.contrib.gis.db import models as gis_models
import uuid
from apps.core.geo_utils import polygon_to_h3_cells, ZONE_RESOLUTION

class OperationalZone(models.Model):
    """
    A geographic zone defined by a polygon boundary.
    Automatically indexed with H3 hexagons for fast lookup.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    
    # PostGIS PolygonField stores the actual shape drawn in Admin
    boundary = gis_models.PolygonField(geography=True, db_index=True)
    
    is_active = models.BooleanField(default=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        is_new_boundary = False
        if not self.pk:
            is_new_boundary = True
        else:
            old_instance = OperationalZone.objects.filter(pk=self.pk).first()
            if old_instance and old_instance.boundary != self.boundary:
                is_new_boundary = True

        super().save(*args, **kwargs)

        if is_new_boundary:
            self.refresh_h3_indexes()

    def refresh_h3_indexes(self):
        """Calculates and stores H3 cells for the boundary."""
        import logging
        from django.db import transaction
        
        logger = logging.getLogger(__name__)
        
        try:
            cells = polygon_to_h3_cells(self.boundary, ZONE_RESOLUTION)
            
            if not cells:
                logger.warning(f"Polygon refresh for zone '{self.name}' returned 0 cells. Aborting index update to prevent data loss.")
                return False

            with transaction.atomic():
                # Clear old indexes only if we have new ones to replace them with
                ZoneH3Index.objects.filter(zone=self).delete()
                
                # Bulk create new indexes
                index_objects = [
                    ZoneH3Index(zone=self, h3_index=cell)
                    for cell in cells
                ]
                ZoneH3Index.objects.bulk_create(index_objects)
                
                # Update metadata for quick reference
                self.metadata['h3_count'] = len(cells)
                OperationalZone.objects.filter(pk=self.pk).update(metadata=self.metadata)
            
            return True
        except Exception as e:
            logger.error(f"Failed to refresh H3 indexes for zone '{self.name}': {str(e)}")
            return False

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']

class ZoneH3Index(models.Model):
    """
    Mapping table between OperationalZones and H3 cells.
    Allows for extremely fast 'Point in Zone' lookups.
    """
    zone = models.ForeignKey(OperationalZone, on_delete=models.CASCADE, related_name='h3_indexes')
    h3_index = models.CharField(max_length=20, db_index=True)
    resolution = models.IntegerField(default=ZONE_RESOLUTION)

    class Meta:
        unique_together = ('zone', 'h3_index')
        verbose_name = "Zone H3 Index"
        verbose_name_plural = "Zone H3 Indexes"
