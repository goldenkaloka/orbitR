from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.geo.models import OperationalZone, ZoneH3Index
from api.serializers.geo import OperationalZoneSerializer, OperationalZoneMinimalSerializer
from apps.core.geo_utils import latlng_to_h3, ZONE_RESOLUTION, get_neighbors

class OperationalZoneViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OperationalZone.objects.filter(is_active=True)
    serializer_class = OperationalZoneSerializer

    @action(detail=False, methods=['GET'])
    def resolve(self, request):
        """
        Resolve a lat/lng to a zone using H3 indexing.
        Params: lat, lng
        """
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        
        if not lat or not lng:
            return Response({'error': 'lat and lng are required'}, status=400)
            
        try:
            h3_index = latlng_to_h3(lat, lng, ZONE_RESOLUTION)
            print(f"🌍 RESOLVING ZONE FOR: Lat {lat}, Lng {lng} -> H3: {h3_index}")
            
            # Find the zone that contains this H3 cell
            zone_index = ZoneH3Index.objects.filter(
                h3_index=h3_index, 
                zone__is_active=True
            ).select_related('zone').first()
            
            if zone_index:
                print(f"✅ FOUND DIRECT MATCH in Zone: {zone_index.zone.name}")
                return Response(OperationalZoneMinimalSerializer(zone_index.zone).data)
            
            print(f"⚠️ NO DIRECT MATCH. Checking neighbors...")
            # If not found, check neighboring cells (for edge cases near zone boundaries)
            neighbor_h3_cells = get_neighbors(h3_index)
            print(f"   Neighbors to check: {neighbor_h3_cells}")
            
            neighbor_zones = ZoneH3Index.objects.filter(
                h3_index__in=neighbor_h3_cells,
                zone__is_active=True
            ).select_related('zone').first()
            
            if neighbor_zones:
                print(f"✅ FOUND NEIGHBOR MATCH in Zone: {neighbor_zones.zone.name}")
                return Response(OperationalZoneMinimalSerializer(neighbor_zones.zone).data)
            
            print(f"❌ COMPLETELY OUT OF BOUNDS: No zones found even in neighbors.")
            return Response({'detail': 'No zone found for these coordinates'}, status=404)
        except (ValueError, TypeError):
            return Response({'error': 'Invalid lat or lng format'}, status=400)

    @action(detail=False, methods=['GET'])
    def autocomplete(self, request):
        """
        Proxy endpoint for location search (autocomplete).
        Uses Mapbox Search Box API first, falls back to Geocoding v5 if no results.
        Search Box API is better for POI search, Geocoding v5 provides broader coverage.
        """
        query = request.query_params.get('q', '')
        if not query or len(query) < 2:
            return Response([])
            
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        
        token = "pk.eyJ1IjoiZ29sZGVua2Fsb2thIiwiYSI6ImNtbnI4OGxpbjAxYmMyb3F5YjV3dXRhOXUifQ.Ch12y0VZXfYo8j8HvFjz4g"
        
        import requests
        import urllib.parse
        
        results = []
        
        try:
            search_box_url = f"https://api.mapbox.com/search/searchbox/v1/forward?access_token={token}&q={urllib.parse.quote(query)}&country=tz&limit=10&types=poi,address,place,locality"
            if lat and lng:
                search_box_url += f"&proximity={lng},{lat}"
            
            response = requests.get(search_box_url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                for feature in data.get('features', []):
                    feature_type = feature.get('properties', {}).get('feature_type', '')
                    props = feature.get('properties', {})
                    
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
                    
                    results.append({
                        'id': props.get('mapbox_id', ''),
                        'name': name,
                        'address': full_address,
                        'lat': coords.get('latitude', 0),
                        'lng': coords.get('longitude', 0),
                        'feature_type': feature_type,
                        'source': 'searchbox',
                    })
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Search Box API failed, trying fallback: {e}")
        
        if not results:
            try:
                geocode_url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{urllib.parse.quote(query)}.json?access_token={token}&country=tz&limit=10"
                if lat and lng:
                    geocode_url += f"&proximity={lng},{lat}"
                
                response = requests.get(geocode_url, timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    for feature in data.get('features', []):
                        place_name = feature.get('text', '')
                        center = feature.get('center', [0, 0])
                        context = [c.get('text') for c in feature.get('context', [])]
                        address = ", ".join(context) if context else feature.get('place_name', '')
                        
                        results.append({
                            'id': feature.get('id', ''),
                            'name': place_name,
                            'address': address,
                            'lat': center[1],
                            'lng': center[0],
                            'feature_type': feature.get('place_type', [''])[0] if feature.get('place_type') else '',
                            'source': 'geocoding',
                        })
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Geocoding fallback also failed: {e}")
        
        return Response(results)

    @action(detail=False, methods=['GET'])
    def reverse_geocode(self, request):
        """
        Proxy endpoint for reverse geocoding (GPS to Address).
        Uses Mapbox Search Box API first, falls back to Geocoding v5 if no results.
        """
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        
        if not lat or not lng:
            return Response({'error': 'lat and lng are required'}, status=400)
            
        token = "pk.eyJ1IjoiZ29sZGVua2Fsb2thIiwiYSI6ImNtbnI4OGxpbjAxYmMyb3F5YjV3dXRhOXUifQ.Ch12y0VZXfYo8j8HvFjz4g"
        
        import requests
        import urllib.parse
        
        results = []
        
        try:
            url = f"https://api.mapbox.com/search/searchbox/v1/reverse?access_token={token}&latitude={lat}&longitude={lng}&limit=5"
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                for feature in data.get('features', []):
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
                    
                    results.append({
                        'id': props.get('mapbox_id', ''),
                        'name': name,
                        'address': full_address,
                        'lat': coords.get('latitude', 0),
                        'lng': coords.get('longitude', 0),
                        'feature_type': feature_type,
                        'source': 'searchbox',
                    })
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Search Box reverse failed, trying fallback: {e}")
        
        if not results:
            try:
                url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{lng},{lat}.json?access_token={token}&types=poi,place,neighborhood,locality"
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    for feature in data.get('features', []):
                        place_name = feature.get('text', '')
                        center = feature.get('center', [0, 0])
                        context = [c.get('text') for c in feature.get('context', [])]
                        address = ", ".join(context) if context else feature.get('place_name', '')
                        
                        results.append({
                            'id': feature.get('id', ''),
                            'name': place_name,
                            'address': address,
                            'lat': center[1],
                            'lng': center[0],
                            'feature_type': feature.get('place_type', [''])[0] if feature.get('place_type') else '',
                            'source': 'geocoding',
                        })
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Geocoding reverse fallback also failed: {e}")
        
        return Response(results)
