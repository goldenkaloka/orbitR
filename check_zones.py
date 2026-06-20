#!/usr/bin/env python
"""
Check operational zones and their H3 coverage.
Run with: python manage.py shell < check_zones.py
Or: python -c "
import os, sys, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
sys.path.insert(0, 'backend')
django.setup()
from apps.geo.models import OperationalZone, ZoneH3Index
for zone in OperationalZone.objects.filter(is_active=True):
    count = ZoneH3Index.objects.filter(zone=zone).count()
    print(f'{zone.name}: {count} H3 cells')
    print(f'  Description: {zone.description}')
    print(f'  Boundary coords: {list(zone.boundary.coords)[:2] if zone.boundary else "None"}...')
    print()
"
"""
import os
import sys

import django

sys.path.insert(0, 'backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.geo.models import OperationalZone, ZoneH3Index

print("=" * 60)
print("OPERATIONAL ZONES IN DATABASE")
print("=" * 60)

zones = OperationalZone.objects.filter(is_active=True)
if not zones:
    print("No active zones found!")
else:
    for zone in zones:
        count = ZoneH3Index.objects.filter(zone=zone).count()
        print(f"\nZone: {zone.name}")
        print(f"  Description: {zone.description}")
        print(f"  H3 Cells: {count}")
        print(f"  Created: {zone.created_at}")

        # Show sample H3 cells
        samples = ZoneH3Index.objects.filter(zone=zone)[:3]
        print(f"  Sample H3 indexes: {[s.h3_index for s in samples]}")

print("\n" + "=" * 60)
print("TEST A SPECIFIC LOCATION")
print("=" * 60)

# Test a location (e.g., Dar es Salaam center)
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3

test_lat = float(input("Enter latitude (or press Enter for -6.7924): ") or "-6.7924")
test_lng = float(input("Enter longitude (or press Enter for 39.2083): ") or "39.2083")

h3_index = latlng_to_h3(test_lat, test_lng, ZONE_RESOLUTION)
print(f"\nTesting coordinates: {test_lat}, {test_lng}")
print(f"H3 index (resolution {ZONE_RESOLUTION}): {h3_index}")

zone_index = ZoneH3Index.objects.filter(h3_index=h3_index, zone__is_active=True).select_related('zone').first()

if zone_index:
    print(f"Found zone: {zone_index.zone.name}")
else:
    print("No zone found for these coordinates!")
    print("Checking neighboring cells...")
    from apps.core.geo_utils import get_neighbors
    neighbors = get_neighbors(h3_index)
    for n in neighbors[:5]:
        nz = ZoneH3Index.objects.filter(h3_index=n, zone__is_active=True).select_related('zone').first()
        if nz:
            print(f"  Neighbor {n}: {nz.zone.name}")