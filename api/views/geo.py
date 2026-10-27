import logging
import os
import urllib.parse

import requests as req_lib
from api.serializers.geo import (
    OperationalZoneMinimalSerializer,
    OperationalZoneSerializer,
)
from apps.core.geo_utils import ZONE_RESOLUTION, get_neighbors, latlng_to_h3
from apps.geo.models import OperationalZone, ZoneH3Index
from apps.geo.place_aliases import alias_coordinates, search_aliases
from django.core.cache import cache
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

logger = logging.getLogger(__name__)

# Read once at import time — avoids repeated os.environ lookups per request.
# Server restart is required to pick up a new token value, which is acceptable.
_MAPBOX_TOKEN: str = (
    os.environ.get('MAPBOX_TOKEN')
    or os.environ.get('MAPBOX_ACCESS_TOKEN', '')
)


def _mapbox_token_or_503(response_factory):
    """Return a 503 Response if the Mapbox token is not configured."""
    if not _MAPBOX_TOKEN:
        return response_factory({'error': 'Geocoding service unavailable'}, status=503)
    return None


def _valid_coords(lat, lng):
    """Return (lat, lng) as floats, or None when the pair is not a real point.

    Mapbox omits `coordinates` on some features. Passing 0,0 through used to
    render as a valid pin in the Gulf of Guinea and could be tapped by mistake.
    """
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
        return None
    if abs(lat_f) < 1e-6 and abs(lng_f) < 1e-6:
        return None
    return lat_f, lng_f


def _parse_searchbox_feature(feature: dict) -> dict | None:
    props = feature.get('properties', {})
    feature_type = props.get('feature_type', '')
    name = props.get('name', '')
    full_address = (props.get('full_address') or props.get('place_formatted') or '').strip()
    if feature_type == 'address':
        name = props.get('address', '')

    coords = props.get('coordinates') or {}
    point = _valid_coords(coords.get('latitude'), coords.get('longitude'))
    if point is None:
        return None

    display_name = (name or full_address).strip()
    if not display_name:
        return None

    return {
        'id': props.get('mapbox_id', ''),
        'name': display_name,
        'address': full_address or name,
        'lat': point[0],
        'lng': point[1],
        'feature_type': feature_type,
        'source': 'searchbox',
    }


def _parse_geocoding_feature(feature: dict) -> dict | None:
    center = feature.get('center') or []
    if len(center) < 2:
        return None
    point = _valid_coords(center[1], center[0])
    if point is None:
        return None

    context = [c.get('text') for c in feature.get('context', []) if c.get('text')]
    text = (feature.get('text') or '').strip()
    place_name = (feature.get('place_name') or '').strip()
    display_name = place_name or text
    if not display_name:
        return None

    return {
        'id': feature.get('id', ''),
        'name': display_name,
        'address': ', '.join(context) if context else place_name,
        'lat': point[0],
        'lng': point[1],
        'feature_type': (feature.get('place_type') or [''])[0],
        'source': 'geocoding',
    }


def _alias_result(alias):
    coordinates = alias_coordinates(alias)
    if not coordinates:
        return None
    return {
        'id': f'alias:{alias.id}',
        'name': alias.display_text,
        'address': (
            f'Community place · {alias.zone.name}'
            if alias.zone_id else 'Community-named place'
        ),
        'lat': coordinates[0],
        'lng': coordinates[1],
        'feature_type': 'alias',
        'source': 'crowd',
    }


class OperationalZoneViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OperationalZone.objects.filter(is_active=True)
    serializer_class = OperationalZoneSerializer

    @action(detail=False, methods=['GET'])
    def resolve(self, request):
        """Resolve a lat/lng to an operational zone using H3 indexing."""
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')

        if not lat or not lng:
            return Response({'error': 'lat and lng are required'}, status=400)

        try:
            h3_index = latlng_to_h3(lat, lng, ZONE_RESOLUTION)
        except (ValueError, TypeError):
            return Response({'error': 'Invalid lat or lng format'}, status=400)

        cache_key = f'geo:zone_resolve:{h3_index}'
        cached = cache.get(cache_key)
        if cached:
            return Response(cached)

        zone_index = ZoneH3Index.objects.filter(
            h3_index=h3_index,
            zone__is_active=True,
        ).select_related('zone').first()

        if zone_index:
            data = OperationalZoneMinimalSerializer(zone_index.zone).data
            cache.set(cache_key, data, 300)
            return Response(data)

        # Expand search to immediate H3 neighbours
        neighbor_h3_cells = get_neighbors(h3_index)
        neighbor_zone = ZoneH3Index.objects.filter(
            h3_index__in=neighbor_h3_cells,
            zone__is_active=True,
        ).select_related('zone').first()

        if neighbor_zone:
            data = OperationalZoneMinimalSerializer(neighbor_zone.zone).data
            cache.set(cache_key, data, 300)
            return Response(data)

        # Fallback: PostGIS spatial containment query.
        # Catches points inside the zone boundary that fall outside the
        # pre-seeded H3 hexagon coverage (~770m radius around core points).
        try:
            from domain.services.geo_services import GeoDomainService
            spatial_zone = GeoDomainService.find_zone_for_point(
                float(lng), float(lat)
            )
            if spatial_zone:
                data = OperationalZoneMinimalSerializer(spatial_zone).data
                cache.set(cache_key, data, 300)
                return Response(data)
        except Exception:
            pass

        cache.set(cache_key, {'detail': 'No zone found for these coordinates'}, 300)
        return Response({'detail': 'No zone found for these coordinates'}, status=404)

    @action(detail=False, methods=['GET'])
    def nearby(self, request):
        """Return zones within a given distance of a lat/lng point."""
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        max_distance = request.query_params.get('max_distance', '30')

        if not lat or not lng:
            return Response({'error': 'lat and lng are required'}, status=400)

        try:
            lat_f = float(lat)
            lng_f = float(lng)
            dist_km = float(max_distance)
        except (ValueError, TypeError):
            return Response({'error': 'Invalid lat, lng or max_distance'}, status=400)

        from django.contrib.gis.geos import Point
        from django.contrib.gis.measure import D

        cache_key = f'geo:zones_nearby:{lat_f}:{lng_f}:{dist_km}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        point = Point(lng_f, lat_f, srid=4326)
        nearby = OperationalZone.objects.filter(
            is_active=True,
            boundary__dwithin=(point, D(km=dist_km)),
        )
        serializer = OperationalZoneMinimalSerializer(nearby, many=True)
        data = serializer.data
        cache.set(cache_key, data, 300)
        return Response(data)

    @action(detail=False, methods=['GET'])
    def autocomplete(self, request):
        """
        Location search. Returns local operational zones first
        (by name match), then Mapbox results as additional suggestions.
        """
        query = request.query_params.get('q', '')
        if not query or len(query) < 2:
            return Response([])

        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')

        cache_key = f"geo:autocomplete:{query}:{lat or ''}:{lng or ''}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        results = []

        # ── Local operational zones first ─────────────────────────────────
        zone_names = set()
        for zone in OperationalZone.objects.filter(is_active=True, name__icontains=query):
            centroid = zone.boundary.centroid if zone.boundary else None
            # A zone without a boundary has no point to fly to. Returning 0,0
            # here used to hand the client an "Africa, Atlantic Ocean" pin.
            if centroid is None:
                continue
            zone_names.add(zone.name.lower())
            results.append({
                'id': str(zone.id),
                'name': zone.name,
                'address': zone.description or '',
                'lat': centroid.y,
                'lng': centroid.x,
                'feature_type': 'zone',
                'source': 'local',
            })

        # ── Crowd-sourced place names ─────────────────────────────────────
        for alias in search_aliases(query, limit=5):
            alias_key = alias.display_text.lower()
            if alias_key in zone_names:
                continue
            payload = _alias_result(alias)
            if not payload:
                continue
            zone_names.add(alias_key)
            results.append(payload)

        # ── Mapbox Search Box (additional suggestions) ────────────────────
        err = _mapbox_token_or_503(Response)
        mapbox_down = False
        if not err:
            try:
                url = (
                    f'https://api.mapbox.com/search/searchbox/v1/forward'
                    f'?access_token={_MAPBOX_TOKEN}'
                    f'&q={urllib.parse.quote(query)}'
                    f'&country=tz&limit=10&types=poi,address,place,locality'
                )
                if lat and lng:
                    url += f'&proximity={lng},{lat}'
                resp = req_lib.get(url, timeout=5)
                if resp.status_code == 200:
                    for feature in resp.json().get('features', []):
                        parsed = _parse_searchbox_feature(feature)
                        if parsed and parsed['name'].lower() not in zone_names:
                            results.append(parsed)
                elif resp.status_code >= 500:
                    mapbox_down = True
            except Exception as exc:
                logger.warning('Search Box autocomplete failed: %s', exc)
                # Circuit-break: a transport failure (timeout/DNS/refused)
                # means the next Mapbox hop will likely fail too — skipping
                # it caps worst-case latency at 5 s instead of 10 s.
                mapbox_down = True

        # ── Geocoding v5 fallback (only if nothing found at all) ──────────
        if not results and not err and not mapbox_down:
            try:
                url = (
                    f'https://api.mapbox.com/geocoding/v5/mapbox.places'
                    f'/{urllib.parse.quote(query)}.json'
                    f'?access_token={_MAPBOX_TOKEN}&country=tz&limit=10'
                )
                if lat and lng:
                    url += f'&proximity={lng},{lat}'
                resp = req_lib.get(url, timeout=5)
                if resp.status_code == 200:
                    results = [
                        parsed for parsed in (
                            _parse_geocoding_feature(f)
                            for f in resp.json().get('features', [])
                        ) if parsed
                    ]
            except Exception as exc:
                logger.error('Geocoding v5 autocomplete fallback failed: %s', exc)

        # Empty results during an outage must not be cached for the full
        # TTL — 30 s bounds both negative-cache poisoning and retry load.
        cache.set(cache_key, results, 300 if results else 30)
        return Response(results)

