"""
Seed 5 restaurants (with sections, foods and images) around Ardhi University,
owned by the admin user 0616622485.

Run inside the Django container:
    docker exec orbitr-django python /app/seed_ardhi_restaurants.py

Idempotent: any restaurant with the same name is removed and re-created.
"""

import os
import urllib.request

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from decimal import Decimal  # noqa: E402

from django.contrib.gis.geos import Point  # noqa: E402
from django.core.files.base import ContentFile  # noqa: E402

from apps.products.models import Food  # noqa: E402
from apps.restaurants.models import Restaurant, RestaurantSection  # noqa: E402
from apps.users.models import User  # noqa: E402

ADMIN_PHONE = "0616622485"
ARU_LAT, ARU_LNG = -6.76472, 39.21417  # Ardhi University

_UA = {"User-Agent": "Mozilla/5.0"}

# ── Image sources ───────────────────────────────────────────────────────────
RESTAURANT_IMG = [
    "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?w=1200&q=70",
    "https://images.unsplash.com/photo-1552566626-52f8b828add9?w=1200&q=70",
    "https://images.unsplash.com/photo-1466978913421-dad2ebd01d17?w=1200&q=70",
    "https://images.unsplash.com/photo-1559339352-11d035aa65de?w=1200&q=70",
    "https://images.unsplash.com/photo-1533777857889-4be7c70b33f7?w=1200&q=70",
]
FOOD_IMG = [
    "https://images.unsplash.com/photo-1504674900247-0877df9cc836?w=800&q=70",
    "https://images.unsplash.com/photo-1567620905732-2d1ec7ab7445?w=800&q=70",
    "https://images.unsplash.com/photo-1565299624946-b28f40a0ae38?w=800&q=70",
    "https://images.unsplash.com/photo-1546069901-ba9599a7e63c?w=800&q=70",
    "https://images.unsplash.com/photo-1555939594-58d7cb561ad1?w=800&q=70",
    "https://images.unsplash.com/photo-1568901346375-23c9450c58cd?w=800&q=70",
    "https://images.unsplash.com/photo-1550547660-d9450f859349?w=800&q=70",
    "https://images.unsplash.com/photo-1540189549336-e6e99c3679fe?w=800&q=70",
    "https://images.unsplash.com/photo-1473093295043-cdd812d0e601?w=800&q=70",
    "https://images.unsplash.com/photo-1621996346565-e3dbc646d9a9?w=800&q=70",
    "https://images.unsplash.com/photo-1603133872878-684f208fb84b?w=800&q=70",
    "https://images.unsplash.com/photo-1512058564366-18510be2db19?w=800&q=70",
    "https://images.unsplash.com/photo-1596797038530-2c107229654b?w=800&q=70",
    "https://images.unsplash.com/photo-1604503468506-a8da13d82791?w=800&q=70",
    "https://images.unsplash.com/photo-1604382354936-07c5d9983bd3?w=800&q=70",
    "https://images.unsplash.com/photo-1574071318508-1cdbab80d002?w=800&q=70",
    "https://images.unsplash.com/photo-1544025162-d76694265947?w=800&q=70",
    "https://images.unsplash.com/photo-1600891964092-4316c288032e?w=800&q=70",
    "https://images.unsplash.com/photo-1504754524776-8f4f37790ca0?w=800&q=70",
    "https://images.unsplash.com/photo-1482049016688-2d3e1b311543?w=800&q=70",
    "https://images.unsplash.com/photo-1551024506-0bccd828d307?w=800&q=70",
    "https://images.unsplash.com/photo-1497034825429-c343d7c6a68f?w=800&q=70",
    "https://images.unsplash.com/photo-1509042239860-f550ce710b93?w=800&q=70",
]


def fetch(url, filename):
    req = urllib.request.Request(url, headers=_UA)
    data = urllib.request.urlopen(req, timeout=40).read()
    return ContentFile(data, name=filename)


def attach(instance, field_name, urls, filename):
    """Download the first working URL into an ImageField (with picsum fallback)."""
    for url in urls:
        try:
            getattr(instance, field_name).save(filename, fetch(url, filename), save=False)
            return True
        except Exception:
            continue
    try:
        getattr(instance, field_name).save(
            filename,
            fetch(f"https://picsum.photos/seed/{filename}/900/700", filename),
            save=False,
        )
        return True
    except Exception as exc:  # pragma: no cover
        print(f"    ! image failed for {filename}: {exc}")
        return False


RESTAURANTS = [
    {
        "name": "Ardhi Campus Kitchen",
        "category": "Wali",
        "price_range": "TSH 3k-8k",
        "rating": Decimal("4.6"),
        "prep": 20,
        "lat": -6.76390,
        "lng": 39.21600,
        "location": "University Road, Observation Hill, Makongo - next to the Ardhi University main gate",
        "description": "Fresh, affordable plates cooked to order: pilau, wali maharage and grilled kuku, right by the Ardhi University main gate.",
        "sections": [
            ("Main Kitchen", "Wali & Pilau"),
            ("Drinks & Juice", "Juice"),
        ],
        "foods": [
            ("Main Kitchen", "Pilau ya Ng'ombe", "Spiced beef pilau with kachumbari", 7000, "POPULAR"),
            ("Main Kitchen", "Wali Maharage", "Steamed rice with coconut beans stew", 4000, "MAIN"),
            ("Main Kitchen", "Kuku Kwa Kupaka", "Grilled chicken in coconut curry", 9000, "MAIN"),
            ("Main Kitchen", "Chipsi Mayai", "Fries omelette with kachumbari", 3500, "SIDES"),
            ("Main Kitchen", "Ndizi Nyama", "Green banana and beef stew", 6500, "MAIN"),
            ("Drinks & Juice", "Fresh Passion Juice", "Freshly squeezed passion fruit", 2500, "DRINKS"),
            ("Drinks & Juice", "Soda 500ml", "Chilled soft drink", 1500, "DRINKS"),
        ],
    },
    {
        "name": "Mama Amina's Nyama Choma",
        "category": "Nyama Choma",
        "price_range": "TSH 5k-15k",
        "rating": Decimal("4.7"),
        "prep": 25,
        "lat": -6.76720,
        "lng": 39.21280,
        "location": "Ardhi University Road, opposite the ARU sports ground, Makongo",
        "description": "Charcoal-grilled nyama choma and mishkaki done the Tanzanian way, a short walk from Ardhi University.",
        "sections": [
            ("Main Kitchen", "Nyama Choma"),
            ("Snacks", "Mishkaki"),
        ],
        "foods": [
            ("Main Kitchen", "Nyama Choma ya Ng'ombe", "Charcoal grilled beef, 1/2 kg", 12000, "POPULAR"),
            ("Main Kitchen", "Kuku Choma", "Whole grilled chicken with kachumbari", 10000, "POPULAR"),
            ("Main Kitchen", "Ndizi Na Nyama", "Green bananas cooked with grilled beef", 5000, "SIDES"),
            ("Snacks", "Mishkaki Wa Ng'ombe (5pcs)", "Grilled beef skewers", 6000, "POPULAR"),
            ("Snacks", "Kachumbari", "Fresh tomato and onion salad", 1500, "SIDES"),
            ("Snacks", "Chipsi", "Fried potatoes", 3000, "SIDES"),
            ("Snacks", "Bottled Water 1L", "Chilled drinking water", 1000, "DRINKS"),
        ],
    },
    {
        "name": "Observation Hill Cafe",
        "category": "Pizza",
        "price_range": "TSH 4k-12k",
        "rating": Decimal("4.4"),
        "prep": 22,
        "lat": -6.76180,
        "lng": 39.21180,
        "location": "Observation Hill, University Road, near the Ardhi University library",
        "description": "Coffee, pizza and burgers in a calm hillside cafe beside the Ardhi University library.",
        "sections": [
            ("Main Kitchen", "Pizza & Burgers"),
            ("Bar", "Coffee"),
        ],
        "foods": [
            ("Main Kitchen", "Margherita Pizza", "Tomato, mozzarella and basil", 11000, "POPULAR"),
            ("Main Kitchen", "BBQ Chicken Pizza", "Grilled chicken, BBQ sauce, peppers", 13000, "MAIN"),
            ("Main Kitchen", "Beef Burger", "Beef patty, cheese, fries", 9000, "MAIN"),
            ("Main Kitchen", "Chicken Burger", "Crispy chicken fillet and slaw", 8500, "MAIN"),
            ("Main Kitchen", "French Fries", "Golden salted fries", 4000, "SIDES"),
            ("Bar", "Cappuccino", "Double espresso with steamed milk", 4500, "DRINKS"),
            ("Bar", "Iced Tea", "Fresh lemon iced tea", 3000, "DRINKS"),
        ],
    },
    {
        "name": "University Road Biryani & Urojo",
        "category": "Urojo",
        "price_range": "TSH 3k-9k",
        "rating": Decimal("4.3"),
        "prep": 18,
        "lat": -6.76930,
        "lng": 39.21760,
        "location": "University Road, Ubungo - 400m from the Ardhi University south gate",
        "description": "Zanzibar-style biryani and the famous Tanga urojo, close to the Ardhi University south gate.",
        "sections": [
            ("Main Kitchen", "Biryani"),
            ("Urojo Corner", "Urojo"),
        ],
        "foods": [
            ("Main Kitchen", "Biryani Ya Kuku", "Spiced chicken biryani with raita", 8500, "POPULAR"),
            ("Main Kitchen", "Samaki Wa Kupaka", "Grilled fish in coconut sauce", 10000, "MAIN"),
            ("Main Kitchen", "Supu Ya Ndizi", "Green banana beef soup", 5000, "MAIN"),
            ("Urojo Corner", "Urojo Special", "Mango urojo with bhajia and potato", 4000, "POPULAR"),
            ("Urojo Corner", "Sambusa (3pcs)", "Beef samosas", 1500, "SIDES"),
            ("Urojo Corner", "Viazi Karai", "Deep-fried spiced potatoes", 2000, "SIDES"),
            ("Urojo Corner", "Madafu", "Fresh coconut water", 2000, "DRINKS"),
        ],
    },
    {
        "name": "Makongo Fast Bites",
        "category": "Burger",
        "price_range": "TSH 2k-7k",
        "rating": Decimal("4.1"),
        "prep": 15,
        "lat": -6.75820,
        "lng": 39.22080,
        "location": "Makongo Juu, Bagamoyo Road corner - 1km from Ardhi University",
        "description": "Quick bites on the go: chipsi mayai, burgers and grilled meat.",
        "sections": [
            ("Main Kitchen", "Fast Food"),
            ("Drinks", "Sodas"),
        ],
        "foods": [
            ("Main Kitchen", "Chipsi Mayai", "The classic fries omelette", 3500, "POPULAR"),
            ("Main Kitchen", "Kuku Wa Kupaka", "Coconut chicken with rice", 7000, "MAIN"),
            ("Main Kitchen", "Burger Ya Nyama", "Beef burger with cheese", 6500, "MAIN"),
            ("Main Kitchen", "Sausage & Chips", "Beef sausage with fries", 4000, "SIDES"),
            ("Main Kitchen", "Mishkaki (3pcs)", "Grilled beef skewers", 3500, "SIDES"),
            ("Drinks", "Soda 500ml", "Assorted cold sodas", 1500, "DRINKS"),
            ("Drinks", "Water 500ml", "Bottled water", 800, "DRINKS"),
        ],
    },
]


def main():
    try:
        admin = User.objects.get(phone=ADMIN_PHONE)
    except User.DoesNotExist:
        raise SystemExit(f"Admin user {ADMIN_PHONE} not found")

    if admin.role != "admin":
        admin.role = "admin"
        admin.save(update_fields=["role"])
        print(f"Set {ADMIN_PHONE} role -> admin")

    names = [r["name"] for r in RESTAURANTS]
    deleted, _ = Restaurant.objects.filter(name__in=names).delete()
    if deleted:
        print(f"Removed {deleted} existing rows for a clean re-seed")

    r_idx = 0
    f_idx = 0
    for data in RESTAURANTS:
        restaurant = Restaurant(
            owner=admin,
            name=data["name"],
            description=data["description"],
            location=data["location"],
            category=data["category"],
            price_range=data["price_range"],
            rating=data["rating"],
            average_prep_time=data["prep"],
            max_delivery_radius_km=6.0,
            is_active=True,
            is_verified_partner=True,
        )
        restaurant.geo_location = Point(data["lng"], data["lat"], srid=4326)
        slug = "".join(c if c.isalnum() else "-" for c in data["name"].lower())
        attach(restaurant, "image", [RESTAURANT_IMG[r_idx % len(RESTAURANT_IMG)]], f"{slug}.jpg")
        attach(restaurant, "logo", [RESTAURANT_IMG[(r_idx + 2) % len(RESTAURANT_IMG)]], f"{slug}-logo.jpg")
        restaurant.save()  # post_save signal creates the default "Main Kitchen" section
        r_idx += 1

        sec_map = {}
        # default section created by the signal
        for sec in restaurant.sections.all():
            sec_map[sec.name] = sec
        for sec_name, spec in data["sections"]:
            if sec_name in sec_map:
                sec = sec_map[sec_name]
                sec.specialization = spec
                sec.save()
            else:
                sec = RestaurantSection.objects.create(
                    restaurant=restaurant, name=sec_name, specialization=spec, prep_time=data["prep"]
                )
            sec_map[sec_name] = sec

        for sec_name, fname, fdesc, price, cat in data["foods"]:
            food = Food(
                section=sec_map[sec_name],
                name=fname,
                description=fdesc,
                price=Decimal(price),
                category=cat,
                is_available=True,
            )
            attach(food, "image", [FOOD_IMG[f_idx % len(FOOD_IMG)]], f"{slug}-food-{f_idx}.jpg")
            food.save()
            f_idx += 1

        print(f"  + {restaurant.name}  ({len(data['foods'])} foods)")

    print("\nDone.")
    print(f"Restaurants: {Restaurant.objects.count()}  Sections: {RestaurantSection.objects.count()}  Foods: {Food.objects.count()}")


if __name__ == "__main__":
    main()
