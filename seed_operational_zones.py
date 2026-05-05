import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3
from apps.restaurants.models import OperationalZone


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
        h3_index = latlng_to_h3(row["lat"], row["lng"], ZONE_RESOLUTION)
        if not h3_index:
            print(f"Skipping {row['name']}: invalid coordinates")
            continue

        _, was_created = OperationalZone.objects.update_or_create(
            name=row["name"],
            defaults={
                "description": row["description"],
                "h3_index": h3_index,
                "is_active": True,
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

    print(
        f"OperationalZone seed complete. created={created}, updated={updated}, "
        f"resolution={ZONE_RESOLUTION}"
    )


if __name__ == "__main__":
    seed_operational_zones()
