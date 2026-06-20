import json

import h3

areas = [
  {
    "id": 1,
    "jina": "Mwenge Bus Terminal",
    "eneo": "Mwenge",
    "latitude": -6.7694,
    "longitude": 39.2236,
    "aina": "Kituo cha Usafiri"
  },
  {
    "id": 2,
    "jina": "Mwenge Woodcarvers Market",
    "eneo": "Mwenge",
    "latitude": -6.7681,
    "longitude": 39.2244,
    "aina": "Soko la Sanaa"
  },
  {
    "id": 3,
    "jina": "Mlimani City Shopping Mall",
    "eneo": "Ubungo / Savei",
    "latitude": -6.7725,
    "longitude": 39.2197,
    "aina": "Kituo cha Biashara"
  },
  {
    "id": 4,
    "jina": "Savei Bus Stop",
    "eneo": "Savei",
    "latitude": -6.7678,
    "longitude": 39.2161,
    "aina": "Kituo cha Usafiri"
  },
  {
    "id": 5,
    "jina": "SkyCity Mall",
    "eneo": "University Road / Savei",
    "latitude": -6.7669,
    "longitude": 39.2155,
    "aina": "Kituo cha Biashara"
  },
  {
    "id": 6,
    "jina": "Mlalakuwa Bus Stop",
    "eneo": "Mlalakuwa",
    "latitude": -6.7633,
    "longitude": 39.2261,
    "aina": "Kituo cha Usafiri"
  },
  {
    "id": 7,
    "jina": "Mlalakuwa Dispensary",
    "eneo": "Mlalakuwa",
    "latitude": -6.7612,
    "longitude": 39.2249,
    "aina": "Kituo cha Afya"
  },
  {
    "id": 8,
    "jina": "Mlalakuwa Bridge",
    "eneo": "Mlalakuwa",
    "latitude": -6.7628,
    "longitude": 39.2285,
    "aina": "Daraja / Miundombinu"
  },
  {
    "id": 9,
    "jina": "Ardhi University Main Entrance",
    "eneo": "University Road",
    "latitude": -6.7655,
    "longitude": 39.2142,
    "aina": "Taasisi ya Elimu"
  },
  {
    "id": 10,
    "jina": "Ardhi University Library",
    "eneo": "Ndani ya Chuo",
    "latitude": -6.7648,
    "longitude": 39.2131,
    "aina": "Maktaba"
  },
  {
    "id": 11,
    "jina": "Playtime Cafeteria (ARU)",
    "eneo": "Ndani ya Chuo",
    "latitude": -6.7651,
    "longitude": 39.2134,
    "aina": "Mgahawa"
  },
  {
    "id": 12,
    "jina": "Makongo Juu Darajani",
    "eneo": "Makongo Juu",
    "latitude": -6.7610,
    "longitude": 39.2153,
    "aina": "Kielelezo cha Njia"
  },
  {
    "id": 13,
    "jina": "KKKT Usharika wa Makongo Juu",
    "eneo": "Makongo Juu",
    "latitude": -6.7589,
    "longitude": 39.2114,
    "aina": "Eneo la Ibada"
  },
  {
    "id": 14,
    "jina": "Grams Restaurant",
    "eneo": "Makongo Juu",
    "latitude": -6.7585,
    "longitude": 39.2111,
    "aina": "Mgahawa"
  },
  {
    "id": 15,
    "jina": "Makongo CCM Bus Stop",
    "eneo": "Makongo CCM",
    "latitude": -6.7562,
    "longitude": 39.2081,
    "aina": "Kituo cha Usafiri"
  },
  {
    "id": 16,
    "jina": "Ofisi ya Serikali ya Mtaa (Makongo CCM)",
    "eneo": "Makongo CCM",
    "latitude": -6.7565,
    "longitude": 39.2075,
    "aina": "Ofisi ya Serikali"
  },
  {
    "id": 17,
    "jina": "Makongo DTV Bus Stop",
    "eneo": "Makongo DTV",
    "latitude": -6.7541,
    "longitude": 39.2039,
    "aina": "Kituo cha Usafiri"
  },
  {
    "id": 18,
    "jina": "Makongo Juu ITV / DTV Tower",
    "eneo": "Makongo DTV",
    "latitude": -6.7535,
    "longitude": 39.2028,
    "aina": "Mnara wa Mawasiliano"
  },
  {
    "id": 19,
    "jina": "Seanna Restaurant",
    "eneo": "Makongo Juu",
    "latitude": -6.7548,
    "longitude": 39.2045,
    "aina": "Mgahawa"
  },
  {
    "id": 20,
    "jina": "Makongo Mwisho Bus Terminal",
    "eneo": "Makongo Mwisho",
    "latitude": -6.7492,
    "longitude": 39.1989,
    "aina": "Kituo cha Usafiri"
  },
  {
    "id": 21,
    "jina": "Soko la Makongo Mwisho",
    "eneo": "Makongo Mwisho",
    "latitude": -6.7488,
    "longitude": 39.1993,
    "aina": "Soko la Chakula"
  },
  {
    "id": 22,
    "jina": "Makongo High School",
    "eneo": "Makongo",
    "latitude": -6.7521,
    "longitude": 39.2005,
    "aina": "Taasisi ya Elimu"
  },
  {
    "id": 23,
    "jina": "Changanyikeni Bus Stop",
    "eneo": "Changanyikeni / Karibu na ARU",
    "latitude": -6.7619,
    "longitude": 39.2058,
    "aina": "Kituo cha Usafiri"
  },
  {
    "id": 24,
    "jina": "KukuKuku Restaurant",
    "eneo": "Mlimani Tower / Savei",
    "latitude": -6.7682,
    "longitude": 39.2174,
    "aina": "Mgahawa"
  },
  {
    "id": 25,
    "jina": "Amell In Dar (PSSSF Tower)",
    "eneo": "Ubungo / Savei",
    "latitude": -6.7741,
    "longitude": 39.2185,
    "aina": "Burudani na Mgahawa"
  },
  {
    "id": 26,
    "jina": "UDSM Main Administration Block",
    "eneo": "UDSM Kampasi Kuu",
    "latitude": -6.7711,
    "longitude": 39.2062,
    "aina": "Ofisi za Chuo"
  },
  {
    "id": 27,
    "jina": "Rwegarulila Water Resources Institute",
    "eneo": "University Road",
    "latitude": -6.7661,
    "longitude": 39.2178,
    "aina": "Taasisi ya Elimu"
  },
  {
    "id": 28,
    "jina": "Tanesco Mwenge Substation",
    "eneo": "Mwenge",
    "latitude": -6.7665,
    "longitude": 39.2255,
    "aina": "Miundombinu"
  },
  {
    "id": 29,
    "jina": "St. Thomas More Catholic Church",
    "eneo": "Mlalakuwa",
    "latitude": -6.7625,
    "longitude": 39.2241,
    "aina": "Eneo la Ibada"
  },
  {
    "id": 30,
    "jina": "Makongo Juu Dispensary",
    "eneo": "Makongo Juu",
    "latitude": -6.7515,
    "longitude": 39.2012,
    "aina": "Kituo cha Afya"
  }
]

H3_RESOLUTION = 9

for area in areas:
    lat = area["latitude"]
    lng = area["longitude"]
    # Calculate the h3 cell index for the given coordinates at resolution 9
    h3_index = h3.geo_to_h3(lat, lng, H3_RESOLUTION)
    area["h3_index"] = h3_index

# Save to a new JSON file
with open("areas_with_h3.json", "w") as f:
    json.dump(areas, f, indent=4)

print("✅ Successfully generated H3 indexes for all areas!")
print("Saved to areas_with_h3.json")

# Also print the first 5 so you can see what it looks like:
print("\nPreview of first 5 items:")
print(json.dumps(areas[:5], indent=2))
