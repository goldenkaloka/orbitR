import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

import h3
from apps.core.geo_utils import ZONE_RESOLUTION
from django.contrib.gis.geos import MultiPolygon, Polygon
from apps.geo.models import OperationalZone, ZoneH3Index

ZONES_DATA = [
    {
        "name": "UDSM & Mlimani",
        "description": "University of Dar es Salaam main campus, hostels, Hall 1-7, Savei, and the Mlimani City shopping mall hub.",
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
    print("Clearing old operational zones...")
    OperationalZone.objects.all().delete()

    # Step 1: Assign core points and generate candidate H3 cell sets for each zone
    # We expand around each core point by k=4 rings (~770m radius)
    candidate_cells = {}
    point_to_zone = {}

    for zone_data in ZONES_DATA:
        zone_name = zone_data["name"]
        candidate_cells[zone_name] = set()
        
        for lat, lng in zone_data["points"]:
            point_to_zone[(lat, lng)] = zone_name
            # Get H3 cell for this point
            core_cell = h3.latlng_to_cell(lat, lng, ZONE_RESOLUTION)
            # Expand by 4 rings to cover the neighborhood
            neighbors = h3.grid_disk(core_cell, 4)
            candidate_cells[zone_name].update(neighbors)

    # Step 2: Build union of all candidate cells and perform Voronoi assignment
    # This guarantees that each candidate H3 cell is assigned to EXACTLY one zone
    all_unique_cells = set()
    for cells in candidate_cells.values():
        all_unique_cells.update(cells)

    final_zone_cells = {zone_data["name"]: set() for zone_data in ZONES_DATA}

    for cell in all_unique_cells:
        cell_lat, cell_lng = h3.cell_to_latlng(cell)
        
        # Find the closest core point to this cell
        closest_point = None
        min_dist = float('inf')
        
        for pt in point_to_zone.keys():
            # Great-circle distance
            dist = h3.great_circle_distance((cell_lat, cell_lng), pt)
            if dist < min_dist:
                min_dist = dist
                closest_point = pt
        
        # Assign exclusively to the zone of the closest core point
        assigned_zone = point_to_zone[closest_point]
        final_zone_cells[assigned_zone].add(cell)

    # Step 3: Create OperationalZones and save clean, disjoint MultiPolygons
    for zone_data in ZONES_DATA:
        name = zone_data["name"]
        description = zone_data["description"]
        cells = list(final_zone_cells[name])

        if not cells:
            print(f"Warning: Zone '{name}' has 0 assigned cells. Skipping.")
            continue

        # Convert H3 cells back to list of GEOS Polygon shapes
        polys = []
        for cell in cells:
            boundary_coords = h3.cell_to_boundary(cell)
            exterior = [(lng, lat) for lat, lng in boundary_coords]
            # Close the ring
            if exterior[0] != exterior[-1]:
                exterior.append(exterior[0])
            polys.append(Polygon(exterior))

        # Take spatial union of all hexagons to create a single contiguous / MultiPolygon boundary
        union_poly = polys[0]
        for p in polys[1:]:
            union_poly = union_poly.union(p)

        if union_poly.geom_type == 'Polygon':
            boundary = MultiPolygon(union_poly)
        elif union_poly.geom_type == 'MultiPolygon':
            boundary = union_poly
        else:
            boundary = MultiPolygon(union_poly.convex_hull)

        # Create zone
        zone = OperationalZone.objects.create(
            name=name,
            description=description,
            boundary=boundary,
            is_active=True
        )

        # Bulk create exclusive ZoneH3Index records
        index_objects = [
            ZoneH3Index(zone=zone, h3_index=cell)
            for cell in cells
        ]
        ZoneH3Index.objects.bulk_create(index_objects)

        # Set count in metadata
        zone.metadata['h3_count'] = len(cells)
        zone.save()

        print(f"Created zone '{zone.name}' with {len(cells)} exclusive hexagons.")

    print("OperationalZone Voronoi normalization seed complete!")

if __name__ == "__main__":
    seed_operational_zones()
