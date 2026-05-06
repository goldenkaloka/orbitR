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
