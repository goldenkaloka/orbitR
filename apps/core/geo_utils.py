import h3

# Constants for H3 resolutions used across the system
# Resolution 9: ~0.1 km^2 (170m edge) - Standard for Delivery Zones and Batching
ZONE_RESOLUTION = 9 

# Resolution 11: ~0.002 km^2 (25m edge) - High precision for Proximity/Arrival checks
PROXIMITY_RESOLUTION = 11

def latlng_to_h3(lat, lng, resolution=ZONE_RESOLUTION):
    """
    Unified helper to convert coordinates to H3 index string.
    Ensures consistent resolution usage.
    """
    if lat is None or lng is None:
        return None
    try:
        return h3.latlng_to_cell(float(lat), float(lng), resolution)
    except Exception:
        return None

def get_neighbors(h3_index, k=1):
    """Get neighboring cells including the origin."""
    try:
        return list(h3.grid_disk(h3_index, k))
    except Exception:
        return [h3_index] if h3_index else []
