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
            
            # Build a __geo_interface__ compliant dict (H3 accepts this)
            geo_dict = {
                "type": "Polygon",
                "coordinates": [exterior]
            }
            
            # H3 v4 uses polygon_to_cells, H3 v3 used polyfill
            if hasattr(h3, 'polygon_to_cells'):
                poly_cells = h3.polygon_to_cells(geo_dict, resolution)
            else:
                poly_cells = h3.polyfill(geo_dict, resolution)
                
            cells.update(poly_cells)
            
        return list(cells)
    except Exception as e:
        logger.error(f"Failed to convert polygon to H3 cells: {str(e)}")
        return []
