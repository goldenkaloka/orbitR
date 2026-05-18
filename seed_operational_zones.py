import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.core.geo_utils import ZONE_RESOLUTION
from django.contrib.gis.geos import Point, MultiPolygon, Polygon
from apps.geo.models import OperationalZone

ZONES_DATA = [
    {
        "name": "UDSM & Mlimani",
        "description": "University of Dar es Salaam main campus, hostels, Hall 1-7, and Mlimani City shopping mall area.",
        "points": [
            (-6.7704, 39.2192),  # Hostels Zone
            (-6.7725, 39.2197),  # Mlimani City Shopping Mall
            (-6.7711, 39.2062),  # UDSM Main Administration Block
            (-6.7741, 39.2185),  # Amell In Dar (PSSSF Tower)
            (-6.7682, 39.2174),  # KukuKuku Restaurant
        ]
    },
    {
        "name": "ARU & Changanyikeni",
        "description": "Ardhi University campus, Changanyikeni student residential areas, and Ndani ya Chuo.",
        "points": [
            (-6.7642888, 39.2146876),  # Main Campus
            (-6.7655, 39.2142),  # Ardhi University Main Entrance
            (-6.7648, 39.2131),  # Ardhi University Library
            (-6.7651, 39.2134),  # Playtime Cafeteria (ARU)
            (-6.7669, 39.2155),  # SkyCity Mall
            (-6.7678, 39.2161),  # Savei Bus Stop
            (-6.7619, 39.2058),  # Changanyikeni Bus Stop
            (-6.7661, 39.2178),  # Rwegarulila Water Resources Institute
        ]
    },
    {
        "name": "Mwenge & Mlalakuwa",
        "description": "Mwenge transport terminal, woodcarvers market, and Mlalakuwa community area.",
        "points": [
            (-6.7694, 39.2236),  # Mwenge Bus Terminal
            (-6.7681, 39.2244),  # Mwenge Woodcarvers Market
            (-6.7633, 39.2261),  # Mlalakuwa Bus Stop
            (-6.7612, 39.2249),  # Mlalakuwa Dispensary
            (-6.7628, 39.2285),  # Mlalakuwa Bridge
            (-6.7665, 39.2255),  # Tanesco Mwenge Substation
            (-6.7625, 39.2241),  # St. Thomas More Catholic Church
        ]
    },
    {
        "name": "Makongo Juu",
        "description": "Makongo Juu hill, Makongo CCM, Makongo DTV, and Makongo Mwisho terminal area.",
        "points": [
            (-6.7595, 39.2102),  # Market Zone (Makongo entrance)
            (-6.7610, 39.2153),  # Makongo Juu Darajani
            (-6.7589, 39.2114),  # KKKT Usharika wa Makongo Juu
            (-6.7585, 39.2111),  # Grams Restaurant
            (-6.7562, 39.2081),  # Makongo CCM Bus Stop
            (-6.7565, 39.2075),  # Ofisi ya Serikali ya Mtaa (Makongo CCM)
            (-6.7541, 39.2039),  # Makongo DTV Bus Stop
            (-6.7535, 39.2028),  # Makongo Juu ITV / DTV Tower
            (-6.7548, 39.2045),  # Seanna Restaurant
            (-6.7492, 39.1989),  # Makongo Mwisho Bus Terminal
            (-6.7488, 39.1993),  # Soko la Makongo Mwisho
            (-6.7521, 39.2005),  # Makongo High School
            (-6.7515, 39.2012),  # Makongo Juu Dispensary
        ]
    }
]


def seed_operational_zones():
    # Clear existing highly redundant zones to make room for normalized ones
    print("Clearing old operational zones...")
    OperationalZone.objects.all().delete()

    created = 0

    for zone_data in ZONES_DATA:
        # Create a single MultiPolygon from the union of buffers around each point in the zone
        boundary_poly = None
        for lat, lng in zone_data["points"]:
            p = Point(lng, lat)
            # Buffer of 0.007 degrees is roughly 770 meters radius, giving excellent regional coverage
            p_buf = p.buffer(0.007)
            if boundary_poly is None:
                boundary_poly = p_buf
            else:
                boundary_poly = boundary_poly.union(p_buf)

        # Convert the union result into a MultiPolygon
        if boundary_poly.geom_type == 'Polygon':
            boundary = MultiPolygon(boundary_poly)
        elif boundary_poly.geom_type == 'MultiPolygon':
            boundary = boundary_poly
        else:
            boundary = MultiPolygon(boundary_poly.convex_hull)

        zone = OperationalZone.objects.create(
            name=zone_data["name"],
            description=zone_data["description"],
            boundary=boundary,
            is_active=True
        )
        created += 1
        
        # Trigger H3 hexagons indexing for this zone
        print(f"Indexing hexagons for {zone.name}...")
        zone.refresh_h3_indexes()

    print(
        f"OperationalZone normalization seed complete. Created {created} optimum zones. "
        f"Resolution={ZONE_RESOLUTION}"
    )


if __name__ == "__main__":
    seed_operational_zones()
