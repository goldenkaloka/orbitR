import os
import logging
import urllib.parse

import requests as req_lib
from django.core.cache import cache
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.geo.models import OperationalZone, ZoneH3Index
from api.serializers.geo import OperationalZoneSerializer, OperationalZoneMinimalSerializer
from apps.core.geo_utils import latlng_to_h3, ZONE_RESOLUTION, get_neighbors

logger = logging.getLogger(__name__)

# Read once at import time — avoids repeated os.environ lookups per request.
# Server restart is required to pick up a new token value, which is acceptable.
_MAPBOX_TOKEN: str = os.environ.get('MAPBOX_TOKEN', '')


def _mapbox_token_or_503(response_factory):
    """Return a 503 Response if the Mapbox token is not configured."""
    if not _MAPBOX_TOKEN:
        return response_factory({'error': 'Geocoding service unavailable'}, status=503)
    return None


def _parse_searchbox_feature(feature: dict) -> dict:
    props = feature.get('properties', {})
    feature_type = props.get('feature_type', '')
    if feature_type == 'poi':
        name = props.get('name', '')
        full_address = props.get('full_address', '') or props.get('place_formatted', '')
    elif feature_type == 'address':
        name = props.get('address', '')
        full_address = props.get('full_address', '')
    else:
        name = props.get('name', '')
        full_address = props.get('place_formatted', '')
    coords = props.get('coordinates', {})
    return {
        'id': props.get('mapbox_id', ''),
        'name': name,
        'address': full_address,
        'lat': coords.get('latitude', 0),
        'lng': coords.get('longitude', 0),
        'feature_type': feature_type,
        'source': 'searchbox',
    }


def _parse_geocoding_feature(feature: dict) -> dict:
    center = feature.get('center', [0, 0])
    context = [c.get('text') for c in feature.get('context', [])]
    return {
        'id': feature.get('id', ''),
        'name': feature.get('text', ''),
        'address': ', '.join(context) if context else feature.get('place_name', ''),
        'lat': center[1],
        'lng': center[0],
        'feature_type': (feature.get('place_type') or [''])[0],
        'source': 'geocoding',
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
            cache.set(cache_key, data, 3600)
            return Response(data)

        # Expand search to immediate H3 neighbours
        neighbor_h3_cells = get_neighbors(h3_index)
        neighbor_zone = ZoneH3Index.objects.filter(
            h3_index__in=neighbor_h3_cells,
            zone__is_active=True,
        ).select_related('zone').first()

        if neighbor_zone:
            data = OperationalZoneMinimalSerializer(neighbor_zone.zone).data
            cache.set(cache_key, data, 3600)
            return Response(data)

        cache.set(cache_key, {'detail': 'No zone found for these coordinates'}, 600)
        return Response({'detail': 'No zone found for these coordinates'}, status=404)

    @action(detail=False, methods=['GET'])
    def autocomplete(self, request):
        """
        Proxy for location search. Tries Mapbox Search Box first,
        falls back to Geocoding v5 if it returns no results.
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

        err = _mapbox_token_or_503(Response)
        if err:
            return err

        results = []

        # ── Search Box API ────────────────────────────────────────────────
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
                results = [
                    _parse_searchbox_feature(f)
                    for f in resp.json().get('features', [])
                ]
        except Exception as exc:
            logger.warning('Search Box autocomplete failed: %s', exc)

        # ── Geocoding v5 fallback ─────────────────────────────────────────
        if not results:
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
                        _parse_geocoding_feature(f)
                        for f in resp.json().get('features', [])
                    ]
            except Exception as exc:
                logger.error('Geocoding v5 autocomplete fallback failed: %s', exc)

        cache.set(cache_key, results, 3600)
        return Response(results)

    @action(detail=False, methods=['GET'])
    def reverse_geocode(self, request):
        """
        Proxy for reverse geocoding (GPS → address).
        Tries Mapbox Search Box first, falls back to Geocoding v5.
        """
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')

        if not lat or not lng:
            return Response({'error': 'lat and lng are required'}, status=400)

        cache_key = f'geo:reverse_geocode:{lat}:{lng}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        err = _mapbox_token_or_503(Response)
        if err:
            return err

        results = []

        # ── Search Box reverse ────────────────────────────────────────────
        try:
            url = (
                f'https://api.mapbox.com/search/searchbox/v1/reverse'
                f'?access_token={_MAPBOX_TOKEN}'
                f'&latitude={lat}&longitude={lng}&limit=5'
            )
            resp = req_lib.get(url, timeout=5)
            if resp.status_code == 200:
                results = [
                    _parse_searchbox_feature(f)
                    for f in resp.json().get('features', [])
                ]
        except Exception as exc:
            logger.warning('Search Box reverse geocode failed: %s', exc)

        # ── Geocoding v5 fallback ─────────────────────────────────────────
        if not results:
            try:
                url = (
                    f'https://api.mapbox.com/geocoding/v5/mapbox.places'
                    f'/{lng},{lat}.json'
                    f'?access_token={_MAPBOX_TOKEN}'
                    f'&types=poi,place,neighborhood,locality'
                )
                resp = req_lib.get(url, timeout=5)
                if resp.status_code == 200:
                    results = [
                        _parse_geocoding_feature(f)
                        for f in resp.json().get('features', [])
                    ]
            except Exception as exc:
                logger.error('Geocoding v5 reverse fallback failed: %s', exc)

        cache.set(cache_key, results, 3600)
        return Response(results)
