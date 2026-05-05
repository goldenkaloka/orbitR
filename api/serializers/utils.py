import json
from django.contrib.gis.geos import Point

def parse_coordinate_dict(data, key='location'):
    """
    Utility to convert a dictionary with latitude/longitude into a PostGIS Point.
    Supports keys: latitude, lat, longitude, lng.
    """
    loc_data = data.get(key)
    if isinstance(loc_data, str):
        try:
            loc_data = json.loads(loc_data)
        except (TypeError, ValueError, json.JSONDecodeError):
            loc_data = None

    if isinstance(loc_data, dict):
        try:
            lat = float(loc_data.get('latitude') or loc_data.get('lat'))
            lng = float(loc_data.get('longitude') or loc_data.get('lng'))
            return Point(lng, lat)
        except (TypeError, ValueError):
            return None

    # Fallback for flat payloads where coordinates are not nested under `location`.
    try:
        lat = float(data.get('latitude') or data.get('lat'))
        lng = float(data.get('longitude') or data.get('lng'))
        return Point(lng, lat)
    except (TypeError, ValueError):
        return None

    return None
