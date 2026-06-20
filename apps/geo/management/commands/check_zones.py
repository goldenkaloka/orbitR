from apps.core.geo_utils import ZONE_RESOLUTION, get_neighbors, latlng_to_h3
from apps.geo.models import OperationalZone, ZoneH3Index
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Check operational zones and test location resolution'

    def add_arguments(self, parser):
        parser.add_argument('--test-lat', type=float, help='Test latitude')
        parser.add_argument('--test-lng', type=float, help='Test longitude')

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS('\n=== OPERATIONAL ZONES ===\n'))
        
        zones = OperationalZone.objects.filter(is_active=True)
        if not zones:
            self.stdout.write(self.style.ERROR('No active zones found!'))
            return

        for zone in zones:
            count = ZoneH3Index.objects.filter(zone=zone).count()
            self.stdout.write(f'Zone: {zone.name}')
            self.stdout.write(f'  Description: {zone.description or "N/A"}')
            self.stdout.write(f'  H3 Cells: {count}')
            
            # Show sample H3 cells
            samples = list(ZoneH3Index.objects.filter(zone=zone)[:3])
            if samples:
                self.stdout.write(f'  Sample H3: {[s.h3_index for s in samples]}')
            self.stdout.write('')

        # Test specific location if provided
        if options['test_lat'] and options['test_lng']:
            lat = options['test_lat']
            lng = options['test_lng']
            
            self.stdout.write(self.style.SUCCESS(f'\n=== TEST LOCATION: {lat}, {lng} ===\n'))
            
            h3_index = latlng_to_h3(lat, lng, ZONE_RESOLUTION)
            self.stdout.write(f'H3 Index (res {ZONE_RESOLUTION}): {h3_index}')
            
            zone_index = ZoneH3Index.objects.filter(
                h3_index=h3_index, 
                zone__is_active=True
            ).select_related('zone').first()
            
            if zone_index:
                self.stdout.write(self.style.SUCCESS(f'✓ Found zone: {zone_index.zone.name}'))
            else:
                self.stdout.write(self.style.ERROR('✗ No zone found!'))
                self.stdout.write('Checking neighbors...')
                
                neighbors = get_neighbors(h3_index)
                for i, n in enumerate(neighbors[:5]):
                    nz = ZoneH3Index.objects.filter(h3_index=n, zone__is_active=True).select_related('zone').first()
                    if nz:
                        self.stdout.write(f'  Neighbor {i+1}: {nz.zone.name} ({n})')