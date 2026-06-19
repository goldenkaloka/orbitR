import requests
import json
import logging
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.contrib.gis.geos import Polygon, MultiPolygon, LineString, MultiLineString
from django.db import transaction

from apps.geo.models import OperationalZone
from apps.pricing.models import ZonePricingTier

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Auto-creates operational zones by fetching ward (admin_level=8) or sub-ward (admin_level=10) boundaries from OpenStreetMap Overpass API.'

    def add_arguments(self, parser):
        parser.add_argument('--bbox', type=str, default='-6.82,39.15,-6.72,39.28',
                            help='Bounding box coordinates (south, west, north, east). Default covers central Dar.')
        parser.add_argument('--level', type=int, default=8, choices=[8, 10],
                            help='OSM administrative boundary level to query: 8 for Wards (Kata), 10 for Sub-wards (Mitaa). Default is 8.')
        parser.add_argument('--limit', type=int, default=0,
                            help='Limit the number of elements to import (default: 0 for unlimited)')
        parser.add_argument('--clear', action='store_true',
                            help='Clear all existing operational zones and pricing tiers before importing')

    def handle(self, *args, **options):
        bbox = options['bbox']
        level = options['level']
        limit = options['limit']
        clear = options['clear']

        if clear:
            self.stdout.write(self.style.WARNING("Clearing all existing operational zones and pricing tiers..."))
            with transaction.atomic():
                ZonePricingTier.objects.all().delete()
                OperationalZone.objects.all().delete()
            self.stdout.write(self.style.SUCCESS("Database cleared successfully."))

        # Query Overpass for admin_level relations
        query = f"""
        [out:json][timeout:90];
        relation["boundary"="administrative"]["admin_level"="{level}"]({bbox});
        out geom;
        """

        endpoints = [
            "https://lz4.overpass-api.de/api/interpreter",
            "https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.nchc.org.tw/api/interpreter"
        ]

        headers = {
            "User-Agent": "MwanachuoDeliveryApp/1.0 (operations@mwanachuo.com)",
            "Content-type": "application/x-www-form-urlencoded"
        }

        response = None
        success = False
        level_name = "Ward" if level == 8 else "Sub-ward"

        self.stdout.write(f"Querying {level_name}s inside bounding box: {bbox}")
        for url in endpoints:
            self.stdout.write(f"Connecting to: {url}...")
            try:
                response = requests.post(url, data={"data": query}, headers=headers, timeout=45)
                response.raise_for_status()
                success = True
                self.stdout.write(self.style.SUCCESS(f"Connected successfully to {url}!"))
                break
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"Failed to fetch from {url}: {e}"))
                continue

        if not success:
            self.stdout.write(self.style.ERROR("Error: All Overpass API mirrors failed to respond."))
            return

        try:
            data = response.json()
        except json.JSONDecodeError:
            self.stdout.write(self.style.ERROR("Error: Received invalid JSON from Overpass API."))
            return

        elements = data.get("elements", [])
        self.stdout.write(f"Found {len(elements)} {level_name} boundaries in OpenStreetMap.")

        if limit > 0:
            self.stdout.write(f"Applying limit: only importing first {limit} elements.")
            elements = elements[:limit]

        imported_count = 0

        for element in elements:
            tags = element.get("tags", {})
            name = tags.get("name")
            if not name:
                continue

            osm_id = element.get("id")
            self.stdout.write(f"Processing {level_name.lower()}: {name} (OSM ID: {osm_id})...")

            # Extract way coordinate arrays
            ways = []
            members = element.get("members", [])
            for member in members:
                if member.get("type") == "way" and member.get("role") in ["outer", ""]:
                    geom = member.get("geometry", [])
                    if len(geom) >= 2:
                        coords = [(pt["lon"], pt["lat"]) for pt in geom]
                        ways.append(coords)

            if not ways:
                self.stdout.write(self.style.WARNING(f"  Skipping {name}: No outer boundaries found."))
                continue

            loops = self.stitch_ways(ways)
            if not loops:
                self.stdout.write(self.style.WARNING(f"  Skipping {name}: Could not stitch segments into closed loops."))
                continue

            polygons = []
            for loop in loops:
                polygons.append(Polygon(loop))

            if not polygons:
                continue

            boundary = MultiPolygon(polygons)

            # Save OperationalZone and Pricing Tier inside a transaction
            try:
                with transaction.atomic():
                    # Create or update OperationalZone
                    zone, created = OperationalZone.objects.update_or_create(
                        name=name,
                        defaults={
                            "boundary": boundary,
                            "description": f"{level_name} imported from OSM (OSM ID: {osm_id})",
                            "is_active": True
                        }
                    )

                    # Create or update the 1500 TSh pricing tier
                    ZonePricingTier.objects.update_or_create(
                        zone=zone,
                        defaults={
                            "per_km_multiplier": Decimal('1.00'),
                            "base_fee_override": Decimal('1500.00')
                        }
                    )

                status_text = "Created" if created else "Updated"
                self.stdout.write(self.style.SUCCESS(f"  ✓ {status_text} zone: {name} as a {level_name} with 1500 TSh base fee override."))
                imported_count += 1

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ Failed to save zone {name}: {e}"))

        self.stdout.write(self.style.SUCCESS(f"\nImport finished! Successfully imported/updated {imported_count} {level_name.lower()} zones."))

    def stitch_ways(self, ways):
        """
        Stitches list of way coordinate arrays end-to-end into closed loops.
        """
        remaining_ways = [list(w) for w in ways if len(w) >= 2]
        loops = []
        TOLERANCE = 1e-6

        def is_close(pt1, pt2):
            return (abs(pt1[0] - pt2[0]) < TOLERANCE) and (abs(pt1[1] - pt2[1]) < TOLERANCE)

        while remaining_ways:
            current_loop = remaining_ways.pop(0)
            progress = True
            
            while progress:
                progress = False
                start_pt = current_loop[0]
                end_pt = current_loop[-1]
                
                for i, way in enumerate(remaining_ways):
                    way_start = way[0]
                    way_end = way[-1]
                    
                    if is_close(end_pt, way_start):
                        current_loop.extend(way[1:])
                        remaining_ways.pop(i)
                        progress = True
                        break
                    elif is_close(end_pt, way_end):
                        current_loop.extend(way[:-1][::-1])
                        remaining_ways.pop(i)
                        progress = True
                        break
                    elif is_close(start_pt, way_end):
                        current_loop = way[:-1] + current_loop
                        remaining_ways.pop(i)
                        progress = True
                        break
                    elif is_close(start_pt, way_start):
                        current_loop = way[1:][::-1] + current_loop
                        remaining_ways.pop(i)
                        progress = True
                        break
                        
            if len(current_loop) >= 3:
                if not is_close(current_loop[0], current_loop[-1]):
                    current_loop.append(current_loop[0])
                loops.append(current_loop)
                
        return loops
