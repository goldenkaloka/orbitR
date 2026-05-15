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
    { "name": "Mwenge Bus Terminal", "description": "Kituo cha Usafiri at Mwenge", "lat": -6.7694, "lng": 39.2236 },
    { "name": "Mwenge Woodcarvers Market", "description": "Soko la Sanaa at Mwenge", "lat": -6.7681, "lng": 39.2244 },
    { "name": "Mlimani City Shopping Mall", "description": "Kituo cha Biashara at Ubungo / Savei", "lat": -6.7725, "lng": 39.2197 },
    { "name": "Savei Bus Stop", "description": "Kituo cha Usafiri at Savei", "lat": -6.7678, "lng": 39.2161 },
    { "name": "SkyCity Mall", "description": "Kituo cha Biashara at University Road / Savei", "lat": -6.7669, "lng": 39.2155 },
    { "name": "Mlalakuwa Bus Stop", "description": "Kituo cha Usafiri at Mlalakuwa", "lat": -6.7633, "lng": 39.2261 },
    { "name": "Mlalakuwa Dispensary", "description": "Kituo cha Afya at Mlalakuwa", "lat": -6.7612, "lng": 39.2249 },
    { "name": "Mlalakuwa Bridge", "description": "Daraja / Miundombinu at Mlalakuwa", "lat": -6.7628, "lng": 39.2285 },
    { "name": "Ardhi University Main Entrance", "description": "Taasisi ya Elimu at University Road", "lat": -6.7655, "lng": 39.2142 },
    { "name": "Ardhi University Library", "description": "Maktaba at Ndani ya Chuo", "lat": -6.7648, "lng": 39.2131 },
    { "name": "Playtime Cafeteria (ARU)", "description": "Mgahawa at Ndani ya Chuo", "lat": -6.7651, "lng": 39.2134 },
    { "name": "Makongo Juu Darajani", "description": "Kielelezo cha Njia at Makongo Juu", "lat": -6.7610, "lng": 39.2153 },
    { "name": "KKKT Usharika wa Makongo Juu", "description": "Eneo la Ibada at Makongo Juu", "lat": -6.7589, "lng": 39.2114 },
    { "name": "Grams Restaurant", "description": "Mgahawa at Makongo Juu", "lat": -6.7585, "lng": 39.2111 },
    { "name": "Makongo CCM Bus Stop", "description": "Kituo cha Usafiri at Makongo CCM", "lat": -6.7562, "lng": 39.2081 },
    { "name": "Ofisi ya Serikali ya Mtaa (Makongo CCM)", "description": "Ofisi ya Serikali at Makongo CCM", "lat": -6.7565, "lng": 39.2075 },
    { "name": "Makongo DTV Bus Stop", "description": "Kituo cha Usafiri at Makongo DTV", "lat": -6.7541, "lng": 39.2039 },
    { "name": "Makongo Juu ITV / DTV Tower", "description": "Mnara wa Mawasiliano at Makongo DTV", "lat": -6.7535, "lng": 39.2028 },
    { "name": "Seanna Restaurant", "description": "Mgahawa at Makongo Juu", "lat": -6.7548, "lng": 39.2045 },
    { "name": "Makongo Mwisho Bus Terminal", "description": "Kituo cha Usafiri at Makongo Mwisho", "lat": -6.7492, "lng": 39.1989 },
    { "name": "Soko la Makongo Mwisho", "description": "Soko la Chakula at Makongo Mwisho", "lat": -6.7488, "lng": 39.1993 },
    { "name": "Makongo High School", "description": "Taasisi ya Elimu at Makongo", "lat": -6.7521, "lng": 39.2005 },
    { "name": "Changanyikeni Bus Stop", "description": "Kituo cha Usafiri at Changanyikeni / Karibu na ARU", "lat": -6.7619, "lng": 39.2058 },
    { "name": "KukuKuku Restaurant", "description": "Mgahawa at Mlimani Tower / Savei", "lat": -6.7682, "lng": 39.2174 },
    { "name": "Amell In Dar (PSSSF Tower)", "description": "Burudani na Mgahawa at Ubungo / Savei", "lat": -6.7741, "lng": 39.2185 },
    { "name": "UDSM Main Administration Block", "description": "Ofisi za Chuo at UDSM Kampasi Kuu", "lat": -6.7711, "lng": 39.2062 },
    { "name": "Rwegarulila Water Resources Institute", "description": "Taasisi ya Elimu at University Road", "lat": -6.7661, "lng": 39.2178 },
    { "name": "Tanesco Mwenge Substation", "description": "Miundombinu at Mwenge", "lat": -6.7665, "lng": 39.2255 },
    { "name": "St. Thomas More Catholic Church", "description": "Eneo la Ibada at Mlalakuwa", "lat": -6.7625, "lng": 39.2241 },
    { "name": "Makongo Juu Dispensary", "description": "Kituo cha Afya at Makongo Juu", "lat": -6.7515, "lng": 39.2012 },
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
