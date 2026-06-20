import json

from apps.restaurants.models import Restaurant
from django.contrib.gis.geos import GEOSGeometry
from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .models import OperationalZone, ZoneH3Index


@method_decorator(csrf_exempt, name='dispatch')
class ZonesGeoJSON(View):
    def get(self, request):
        zones = OperationalZone.objects.filter(is_active=True)
        
        features = []
        colors = [
            '#4285f4', '#ea4335', '#34a853', '#fbbc04', '#9334e6',
            '#00acc1', '#ff6d00', '#7cb342', '#c2185b', '#5d4037'
        ]
        
        for idx, zone in enumerate(zones):
            if not zone.boundary:
                continue
                
            try:
                boundary_geojson = json.loads(zone.boundary.geojson)
            except json.JSONDecodeError:
                boundary_geojson = None
                
            if not boundary_geojson:
                continue
            
            h3_count = zone.h3_indexes.count()
            
            zone_restaurants = Restaurant.objects.filter(
                h3_index__in=ZoneH3Index.objects.filter(zone=zone).values('h3_index')
            ).count()
            
            pricing_tier = getattr(zone, 'pricing_tier', None)
            multiplier = float(pricing_tier.per_km_multiplier) if pricing_tier and pricing_tier.per_km_multiplier else 1.0
            
            feature = {
                'type': 'Feature',
                'id': str(zone.id),
                'geometry': boundary_geojson,
                'properties': {
                    'name': zone.name,
                    'is_active': zone.is_active,
                    'description': zone.description or '',
                    'h3_cell_count': h3_count,
                    'restaurant_count': zone_restaurants,
                    'pricing_multiplier': multiplier,
                    'color': colors[idx % len(colors)]
                }
            }
            features.append(feature)
        
        geojson = {
            'type': 'FeatureCollection',
            'features': features
        }
        
        return JsonResponse(geojson, content_type='application/json')