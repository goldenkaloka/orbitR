from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.geo.models import OperationalZone, ZoneH3Index
from api.serializers.geo import OperationalZoneSerializer, OperationalZoneMinimalSerializer
from apps.core.geo_utils import latlng_to_h3, ZONE_RESOLUTION

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
            
            # Find the zone that contains this H3 cell
            zone_index = ZoneH3Index.objects.filter(
                h3_index=h3_index, 
                zone__is_active=True
            ).select_related('zone').first()
            
            if zone_index:
                return Response(OperationalZoneMinimalSerializer(zone_index.zone).data)
            
            return Response({'detail': 'No zone found for these coordinates'}, status=404)
        except (ValueError, TypeError):
            return Response({'error': 'Invalid lat or lng format'}, status=400)

    @action(detail=False, methods=['GET'])
    def autocomplete(self, request):
        """
        Proxy endpoint for Mapbox forward geocoding (search autocomplete).
        Keeps Mapbox tokens secure and parses response for the mobile app.
        """
        query = request.query_params.get('q', '')
        if not query or len(query) < 2:
            return Response([])
            
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        
        # In a real production setup, this moves to settings.py
        token = "pk.eyJ1IjoiZ29sZGVua2Fsb2thIiwiYSI6ImNtbnI4OGxpbjAxYmMyb3F5YjV3dXRhOXUifQ.Ch12y0VZXfYo8j8HvFjz4g"
        
        import requests
        import urllib.parse
        
        url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{urllib.parse.quote(query)}.json?access_token={token}&country=tz&types=poi,place,address,neighborhood,locality"
        
        if lat and lng:
            url += f"&proximity={lng},{lat}"
            
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                results = []
                for feature in data.get('features', []):
                    # We prefer the short text name as primary
                    place_name = feature.get('text', '')
                    
                    # Mapbox 'center' is [longitude, latitude]
                    center = feature.get('center', [0, 0])
                    
                    # Context gives us region/city for the subtitle
                    context = [c.get('text') for c in feature.get('context', [])]
                    address = ", ".join(context) if context else feature.get('place_name', '')
                    
                    results.append({
                        'id': feature.get('id'),
                        'name': place_name,
                        'address': address,
                        'lat': center[1],
                        'lng': center[0],
                    })
                return Response(results)
            return Response({'error': 'Mapbox API error', 'details': response.text}, status=502)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Autocomplete Error: {e}")
            return Response({'error': str(e)}, status=500)
