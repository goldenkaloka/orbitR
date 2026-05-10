import json
from django.contrib.gis.geos import Point

def parse_coordinate_dict(data, key='location'):
    """
    Utility to convert a dictionary with latitude/longitude into a PostGIS Point.
    Supports keys: latitude, lat, longitude, lng.
    Also supports WKT strings like 'POINT(39.2026 -6.7924)'.
    """
    loc_data = data.get(key)
    
    # 1. Handle WKT strings (e.g. from GeoJSON or certain mobile clients)
    if isinstance(loc_data, str) and loc_data.startswith('POINT'):
        from django.contrib.gis.geos import fromstr
        try:
            return fromstr(loc_data)
        except (ValueError, TypeError):
            return None

    # 2. Handle JSON strings
    if isinstance(loc_data, str):
        try:
            loc_data = json.loads(loc_data)
        except (TypeError, ValueError, json.JSONDecodeError):
            # If it's not JSON and didn't match POINT, it might be a flat payload
            pass

    if isinstance(loc_data, dict):
        try:
            lat = float(loc_data.get('latitude') or loc_data.get('lat'))
            lng = float(loc_data.get('longitude') or loc_data.get('lng'))
            return Point(lng, lat)
        except (TypeError, ValueError):
            return None

    # Fallback for flat payloads where coordinates are not nested under `location`.
    try:
        lat = float(data.get('latitude') or data.get('lat') or data.get(f'{key}_latitude'))
        lng = float(data.get('longitude') or data.get('lng') or data.get(f'{key}_longitude'))
        return Point(lng, lat)
    except (TypeError, ValueError):
        pass

    # Multipart/form-data clients may encode nested values as bracket keys.
    try:
        lat = float(data.get(f'{key}[latitude]') or data.get(f'{key}[lat]'))
        lng = float(data.get(f'{key}[longitude]') or data.get(f'{key}[lng]'))
        return Point(lng, lat)
    except (TypeError, ValueError):
        return None

    return None

