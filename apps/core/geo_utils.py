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
        from apps.geo.models import OperationalZone, ZoneH3Index
        zone_h3 = ZoneH3Index.objects.filter(
            h3_index=h3_index,
            zone__is_active=True
        ).select_related('zone').first()
        return zone_h3.zone if zone_h3 else None
    except Exception as e:
        logger.error(f"Failed to get zone from H3 index {h3_index}: {str(e)}")
        return None


# Fix #18 — cache TTL for zone H3 lookups.
# These are called 4-6 times per batching cycle. A short TTL (60s)
# dramatically reduces ZoneH3Index table scans with no meaningful staleness risk.
_ZONE_CACHE_TTL = 60


def get_zone_ids_from_h3(h3_index, radius=1):
    """
    Get a list of zone IDs that cover the given H3 index and its neighbors.
    Returns list of zone IDs. Results are cached for 60 seconds.
    """
    if not h3_index:
        return []
    try:
        from django.core.cache import cache
        cache_key = f"zone_ids_h3:{h3_index}:r{radius}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        neighbor_cells = get_neighbors(h3_index, radius)
        if not neighbor_cells:
            neighbor_cells = [h3_index]

        from apps.geo.models import ZoneH3Index
        zone_ids = list(
            ZoneH3Index.objects.filter(
                h3_index__in=neighbor_cells,
                zone__is_active=True,
            ).values_list('zone_id', flat=True).distinct()
        )

        cache.set(cache_key, zone_ids, _ZONE_CACHE_TTL)
        return zone_ids
    except Exception as e:
        logger.error(f"Failed to get zone IDs from H3 index {h3_index}: {str(e)}")
        return []


def get_neighboring_zone_ids(h3_index, max_distance_km=30):
    """
    Get zone IDs of neighboring zones within a maximum distance.
    Used for inter-zone batching — finds zones that can share batches.

    Fix #12 — dwithin unit mismatch:
    The original code passed `max_distance_km * 1000` as raw metres, but
    GeoDjango's dwithin on a geometry (non-geography) field expects DEGREES.
    Using D(km=...) from django.contrib.gis.measure lets Django handle the
    unit conversion correctly regardless of whether the field is geometry or
    geography.

    Returns dict with 'same_zone' and 'neighbor_zones' lists.
    """
    if not h3_index:
        return {'same_zone': [], 'neighbor_zones': []}

    try:
        from apps.geo.models import OperationalZone
        from django.contrib.gis.geos import Point
        from django.contrib.gis.measure import D
        from django.core.cache import cache

        cache_key = f"neighbor_zones:{h3_index}:d{max_distance_km}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        current_zone = get_zone_from_h3(h3_index)
        if not current_zone:
            result = {'same_zone': [], 'neighbor_zones': []}
            cache.set(cache_key, result, _ZONE_CACHE_TTL)
            return result

        lat, lng = h3.cell_to_latlng(h3_index)
        point = Point(lng, lat, srid=4326)

        # Fix #12: D(km=...) handles unit conversion correctly for both
        # geometry (degrees) and geography (metres) field types.
        nearby_zones = OperationalZone.objects.filter(
            is_active=True,
            boundary__dwithin=(point, D(km=max_distance_km)),
        ).exclude(id=current_zone.id)

        result = {
            'same_zone': [current_zone.id],
            'neighbor_zones': list(nearby_zones.values_list('id', flat=True)),
        }
        cache.set(cache_key, result, _ZONE_CACHE_TTL)
        return result

    except Exception as e:
        logger.error(f"Failed to get neighboring zones: {str(e)}")
        return {'same_zone': [], 'neighbor_zones': []}


def get_zone_h3_cells(zone_ids) -> set:
    """
    Get all H3 cells belonging to the given zone IDs.
    Returns set of H3 index strings.

    Fix #18 — cached for 60 seconds to avoid repeated ZoneH3Index table scans.
    Each batching cycle previously called this 4-6 times with no caching.
    """
    if not zone_ids:
        return set()
    try:
        from django.core.cache import cache
        sorted_ids = tuple(sorted(str(z) for z in zone_ids))
        cache_key = f"zone_h3_cells:{'_'.join(sorted_ids)}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        from apps.geo.models import ZoneH3Index
        cells = set(
            ZoneH3Index.objects.filter(
                zone_id__in=zone_ids
            ).values_list('h3_index', flat=True).distinct()
        )
        cache.set(cache_key, cells, _ZONE_CACHE_TTL)
        return cells
    except Exception as e:
        logger.error(f"Failed to get zone H3 cells: {str(e)}")
        return set()


def haversine_distance(p1, p2) -> float:
    """
    Calculate the great-circle distance between two points on the Earth
    specified as GEOS Point objects (lng, lat).
    Returns distance in meters.
    """
    if not p1 or not p2:
        return 0.0
    # Earth radius in meters
    R = 6371000.0
    
    lng1, lat1 = math.radians(p1.x), math.radians(p1.y)
    lng2, lat2 = math.radians(p2.x), math.radians(p2.y)
    
    dlng = lng2 - lng1
    dlat = lat2 - lat1
    
    a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    
    return R * c



def h3_to_latlng(h3_index):
    """Centre point of an H3 cell as (lat, lng). Raises ValueError if invalid."""
    if not _is_valid_cell(h3_index):
        raise ValueError(f'Invalid H3 cell: {h3_index!r}')
    lat, lng = h3.cell_to_latlng(h3_index)
    return float(lat), float(lng)
