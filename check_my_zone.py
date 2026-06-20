import os
import sys

import django
import h3

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3
from apps.geo.models import OperationalZone, ZoneH3Index
from apps.restaurants.models import Restaurant


def check_zone(lat, lng):
    print("=========================================")
    print(f"🔍 CHECKING YOUR LOCATION: Lat {lat}, Lng {lng}")
    print("=========================================")
    
    # 1. Calculate your H3 Index
    my_h3 = latlng_to_h3(lat, lng, ZONE_RESOLUTION)
    print(f"\n📍 Your H3 Index (Resolution {ZONE_RESOLUTION}): {my_h3}")
    
    # 2. List all Active Zones
    print("\n🗺️  ACTIVE OPERATIONAL ZONES IN DATABASE:")
    zones = OperationalZone.objects.filter(is_active=True)
    if not zones.exists():
        print("  ❌ No active operational zones found in the database!")
    else:
        for z in zones:
            h3_count = z.h3_indexes.count()
            print(f"  ✅ {z.name} (Contains {h3_count} H3 Hexagons)")
            
    # 3. Check Gate 1: Are you in any zone?
    print("\n🚪 GATE 1: ZONE CHECK")
    in_zone = ZoneH3Index.objects.filter(
        h3_index=my_h3,
        resolution=ZONE_RESOLUTION,
        zone__is_active=True
    ).select_related('zone').first()
    
    if in_zone:
        print(f"  ✅ SUCCESS: You are inside '{in_zone.zone.name}'!")
    else:
        print(f"  ❌ FAILED: Your H3 index ({my_h3}) is NOT in any active zone.")
        print("     Reason: The seeded H3 hexagons do not cover your exact GPS coordinates.")
        
    # 4. Check Gate 2: Distance to Restaurants
    print("\n🍔 GATE 2: RESTAURANT DISTANCE CHECK")
    restaurants = Restaurant.objects.filter(is_active=True)
    if not restaurants.exists():
        print("  ❌ No active restaurants found.")
    else:
        for r in restaurants:
            if not r.geo_location:
                print(f"  ⚠️ {r.name}: Has no geo_location set in DB.")
                continue
                
            r_lat = r.geo_location.y
            r_lng = r.geo_location.x
            
            distance_km = h3.great_circle_distance((r_lat, r_lng), (lat, lng))
            
            if distance_km <= r.max_delivery_radius_km:
                print(f"  ✅ {r.name}: {distance_km:.2f}km away (Allowed, max is {r.max_delivery_radius_km}km)")
            else:
                print(f"  ❌ {r.name}: {distance_km:.2f}km away (TOO FAR, max is {r.max_delivery_radius_km}km)")
                
    print("\n=========================================\n")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python check_my_zone.py <latitude> <longitude>")
        print("Example: python check_my_zone.py -6.7642888 39.2146876")
        sys.exit(1)
        
    try:
        input_lat = float(sys.argv[1])
        input_lng = float(sys.argv[2])
        check_zone(input_lat, input_lng)
    except ValueError:
        print("Error: Latitude and Longitude must be numbers.")
