import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3
from django.contrib.gis.geos import Point, MultiPolygon
from apps.geo.models import OperationalZone


SEED_POINTS = [
    {
        "name": "Main Campus",
        "description": "Primary service zone around central campus.",
        "lat": -6.7642888,
        "lng": 39.2146876,
    },
    {
        "name": "Hostels Zone",
        "description": "Dorms and nearby residences.",
        "lat": -6.7704,
        "lng": 39.2192,
    },
    {
        "name": "Market Zone",
        "description": "Commercial and student marketplace area.",
        "lat": -6.7595,
        "lng": 39.2102,
    },
]


def seed_operational_zones():
    created = 0
    updated = 0

    for row in SEED_POINTS:
        # Create a large enough polygon to guarantee H3 resolution 9 centroid captures
        # 0.015 degrees is roughly 1.6km radius
        p = Point(row["lng"], row["lat"])
        boundary = MultiPolygon(p.buffer(0.015)) 

        zone, was_created = OperationalZone.objects.update_or_create(
            name=row["name"],
            defaults={
                "description": row["description"],
                "boundary": boundary,
                "is_active": True,
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1
            
        # FORCE REGENERATE HEXAGONS:
        # Since the geometry didn't change from the broken run, the model's save() 
        # skipped the regeneration. We must force it here.
        zone.refresh_h3_indexes()

    print(
        f"OperationalZone seed complete. created={created}, updated={updated}, "
        f"resolution={ZONE_RESOLUTION}"
    )


if __name__ == "__main__":
    seed_operational_zones()
