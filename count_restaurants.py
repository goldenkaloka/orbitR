import os
import sys

import django

# Set up Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.restaurants.models import Restaurant


def check_restaurants():
    print("=" * 70)
    print("🔍 ORBITR BACKEND RESTAURANT AUDIT")
    print("=" * 70)

    restaurants = Restaurant.objects.all()
    total_count = restaurants.count()
    active_count = restaurants.filter(is_active=True).count()
    inactive_count = total_count - active_count

    print(f"📊 SUMMARY:")
    print(f"  • Total Restaurants: {total_count}")
    print(f"  • Active: {active_count}")
    print(f"  • Inactive: {inactive_count}")
    print("-" * 70)

    if total_count == 0:
        print("❌ No restaurants found in the database. Please run seed scripts to populate.")
    else:
        print(f"📋 DETAILS FOR ALL RESTAURANTS:")
        for idx, r in enumerate(restaurants, 1):
            status_icon = "🟢" if r.is_active else "🔴"
            geo_str = "None"
            h3_str = r.h3_index if r.h3_index else "None"
            
            if r.geo_location:
                # PostGIS geo_location is Point(longitude, latitude)
                geo_str = f"Point({r.geo_location.y:.5f}, {r.geo_location.x:.5f}) [Lat, Lng]"
                if abs(r.geo_location.x) < 0.0001 and abs(r.geo_location.y) < 0.0001:
                    geo_str += " ⚠️ Point(0,0) Invalid!"
            else:
                geo_str += " ❌ Missing!"

            print(f"\n{idx}. {status_icon} {r.name}")
            print(f"   • ID: {r.id}")
            print(f"   • Location Text: {r.location}")
            print(f"   • Geolocation: {geo_str}")
            print(f"   • H3 Index: {h3_str}")
            print(f"   • Max Delivery Radius: {r.max_delivery_radius_km} km")
            print(f"   • Commission Rate: {r.commission_rate * 100:.1f}%")
            print(f"   • Price Range: {r.price_range}")
            print(f"   • Rating: {r.rating:.1f}")

    print("=" * 70)

if __name__ == '__main__':
    check_restaurants()
