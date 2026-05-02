from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from apps.geo.models import OperationalZone
from api.serializers.geo import OperationalZoneSerializer, OperationalZoneMinimalSerializer
from domain.services.geo_services import GeoDomainService

class OperationalZoneViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OperationalZone.objects.filter(is_active=True)
    serializer_class = OperationalZoneSerializer

    @action(detail=False, methods=['GET'])
    def resolve(self, request):
        """
        Resolve a lat/lng to a zone.
        Params: lat, lng
        """
        lat = request.query_params.get('lat')
        lng = request.query_params.get('lng')
        
        if not lat or not lng:
            return Response({'error': 'lat and lng are required'}, status=400)
            
        try:
            zone = GeoDomainService.find_zone_for_point(float(lng), float(lat))
            if zone:
                return Response(OperationalZoneMinimalSerializer(zone).data)
            return Response({'detail': 'No zone found for these coordinates'}, status=404)
        except (ValueError, TypeError):
            return Response({'error': 'Invalid lat or lng format'}, status=400)
