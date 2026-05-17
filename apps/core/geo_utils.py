import logging
import math
from typing import Optional

import h3

# Constants for H3 resolutions used across the system
# Resolution 9: ~0.1 km^2 (170m edge) - Standard for Delivery Zones and Batching
ZONE_RESOLUTION = 9 

# Resolution 11: ~0.002 km^2 (25m edge) - High precision for Proximity/Arrival checks
PROXIMITY_RESOLUTION = 11

logger = logging.getLogger(__name__)

_MIN_RESOLUTION = 0
_MAX_RESOLUTION = 15
_MAX_NEIGHBOR_K = 5


def _parse_finite_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _is_valid_cell(cell: object) -> bool:
    if not isinstance(cell, str) or not cell:
        return False
    is_valid = getattr(h3, "is_valid_cell", None)
    if callable(is_valid):
        try:
            return bool(is_valid(cell))
        except Exception:
            return False
    try:
        # Fallback validation for older API variants.
        h3.cell_to_latlng(cell)
        return True
    except Exception:
        return False

def latlng_to_h3(lat, lng, resolution=ZONE_RESOLUTION):
    """
    Unified helper to convert coordinates to H3 index string.
    Ensures consistent resolution usage.
    """
    if lat is None or lng is None:
        return None
    lat_f = _parse_finite_float(lat)
    lng_f = _parse_finite_float(lng)
    if lat_f is None or lng_f is None:
        return None

    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
        return None

    try:
        res_i = int(resolution)
    except (TypeError, ValueError):
        return None
    if not (_MIN_RESOLUTION <= res_i <= _MAX_RESOLUTION):
        return None

    try:
        return h3.latlng_to_cell(lat_f, lng_f, res_i)
    except Exception:
        logger.warning("Failed to convert lat/lng to H3 cell", extra={"resolution": res_i})
        return None

def get_neighbors(h3_index, k=1):
    """Get neighboring cells including the origin."""
    if not _is_valid_cell(h3_index):
        return []
    try:
        k_i = int(k)
    except (TypeError, ValueError):
        k_i = 1
    if k_i < 0:
        k_i = 0
    if k_i > _MAX_NEIGHBOR_K:
        logger.warning("Clamping H3 neighbor k", extra={"k": k_i, "max_k": _MAX_NEIGHBOR_K})
        k_i = _MAX_NEIGHBOR_K
    try:
        return list(h3.grid_disk(h3_index, k_i))
    except Exception:
        # Safer default: return empty so callers can decide fallback behavior.
        return []


def polygon_to_h3_cells(polygon, resolution=ZONE_RESOLUTION):
    """
    Converts a GEOS Polygon to a list of H3 cell indexes at the given resolution.
    Used for 'filling' a drawn zone with hexagons.
    """
    if not polygon:
        return []
    
    try:
        cells = set()
        
        # A MultiPolygon is a collection of Polygons. We need to process each one.
        # If it happens to be a single Polygon, we can just put it in a list.
        polygons = polygon if polygon.geom_type == 'MultiPolygon' else [polygon]
        
        for poly in polygons:
            # Prepare coordinates for H3 (H3 expects [[lat, lng], ...])
            # GeoDjango Polygon exterior ring is poly[0] or poly.exterior_ring
            # GEOS coords are (lng, lat)
            exterior_ring = poly.exterior_ring if hasattr(poly, 'exterior_ring') else poly[0]
            exterior = [(p[1], p[0]) for p in exterior_ring.coords]
            
            # H3 v4 requires an H3Shape object like LatLngPoly
            if hasattr(h3, 'LatLngPoly'):
                h3_poly = h3.LatLngPoly(exterior)
                poly_cells = h3.polygon_to_cells(h3_poly, resolution)
            else:
                # Fallback for H3 v3 using geojson dict
                geo_dict = {"type": "Polygon", "coordinates": [exterior]}
                poly_cells = h3.polyfill(geo_dict, resolution)
                
            cells.update(poly_cells)
            
        return list(cells)
    except Exception as e:
        logger.error(f"Failed to convert polygon to H3 cells: {str(e)}")
        return []


def get_zone_from_h3(h3_index):
    """
    Get the OperationalZone that contains a given H3 index.
    Returns the zone if found, None otherwise.
    """
    if not h3_index:
        return None
    try:
        from apps.geo.models import ZoneH3Index, OperationalZone
        zone_h3 = ZoneH3Index.objects.filter(
            h3_index=h3_index,
            zone__is_active=True
        ).select_related('zone').first()
        return zone_h3.zone if zone_h3 else None
    except Exception as e:
        logger.error(f"Failed to get zone from H3 index {h3_index}: {str(e)}")
        return None


def get_zone_ids_from_h3(h3_index, radius=1):
    """
    Get a list of zone IDs that cover the given H3 index and its neighbors.
    Returns list of zone IDs.
    """
    if not h3_index:
        return []
    try:
        neighbor_cells = get_neighbors(h3_index, radius)
        if not neighbor_cells:
            neighbor_cells = [h3_index]
        
        from apps.geo.models import ZoneH3Index
        zone_ids = ZoneH3Index.objects.filter(
            h3_index__in=neighbor_cells,
            zone__is_active=True
        ).values_list('zone_id', flat=True).distinct()
        
        return list(zone_ids)
    except Exception as e:
        logger.error(f"Failed to get zone IDs from H3 index {h3_index}: {str(e)}")
        return []


def get_neighboring_zone_ids(h3_index, max_distance_km=30):
    """
    Get zone IDs of neighboring zones within a maximum distance.
    Used for inter-zone batching - finds zones that can share batches.
    
    Returns dict with 'same_zone' and 'neighbor_zones' lists.
    """
    if not h3_index:
        return {'same_zone': [], 'neighbor_zones': []}
    
    try:
        from apps.geo.models import OperationalZone, ZoneH3Index
        
        current_zone = get_zone_from_h3(h3_index)
        if not current_zone:
            return {'same_zone': [], 'neighbor_zones': []}
        
        current_zone_ids = [current_zone.id]
        
        lat, lng = h3.cell_to_latlng(h3_index)
        
        nearby_zones = OperationalZone.objects.filter(
            is_active=True,
            boundary__dwithin=(lng, lat, max_distance_km * 1000)
        ).exclude(id=current_zone.id)
        
        neighbor_zone_ids = list(nearby_zones.values_list('id', flat=True))
        
        return {
            'same_zone': current_zone_ids,
            'neighbor_zones': neighbor_zone_ids
        }
    except Exception as e:
        logger.error(f"Failed to get neighboring zones: {str(e)}")
        return {'same_zone': [], 'neighbor_zones': []}


def get_zone_h3_cells(zone_ids):
    """
    Get all H3 cells belonging to the given zone IDs.
    Returns set of H3 index strings.
    """
    if not zone_ids:
        return set()
    try:
        from apps.geo.models import ZoneH3Index
        cells = ZoneH3Index.objects.filter(
            zone_id__in=zone_ids
        ).values_list('h3_index', flat=True).distinct()
        return set(cells)
    except Exception as e:
        logger.error(f"Failed to get zone H3 cells: {str(e)}")
        return set()
