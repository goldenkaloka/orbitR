import logging

from apps.geo.models import OperationalZone
from django.contrib.gis.geos import Point

logger = logging.getLogger(__name__)

class GeoDomainService:
    """
    Domain logic for spatial operations.
    Strictly focuses on 'What zone is this point in?' logic.
    """

    @staticmethod
    def find_zone_for_point(lng: float, lat: float) -> OperationalZone:
        """
        Identify the OperationalZone that contains the given coordinates.
        Returns None if no active zone matches.
        Standard order: longitude, latitude.
        """
        point = Point(lng, lat)
        
        # Querying with __contains on a PolygonField uses the spatial index
        zone = OperationalZone.objects.filter(
            is_active=True,
            boundary__contains=point
        ).first()
        
        if zone:
            logger.info(f"Point ({lat}, {lng}) resolved to Zone: {zone.name}")
        else:
            logger.warning(f"Point ({lat}, {lng}) does not fall within any operational zone.")
            
        return zone

    @staticmethod
    def is_point_in_zone(lng: float, lat: float, zone_id: str) -> bool:
        """Checks if a specific point is within a specific zone."""
        point = Point(lng, lat)
        return OperationalZone.objects.filter(
            id=zone_id,
            is_active=True,
            boundary__contains=point
        ).exists()
