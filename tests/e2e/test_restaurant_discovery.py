from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.restaurants.models import Restaurant
from apps.users.models import User

LAT = -6.7924
LNG = 39.2083


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class RestaurantDiscoveryTests(TestCase):
    """Ringed discovery for the customer feed.

    Results are distance-ordered, and the search ring widens until it holds a
    usable page — so a customer in a quiet area sees the closest kitchens
    instead of an empty list.
    """

    _origin_seq = 0

    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            phone="+255670000101",
            username="discovery-owner",
            password="pass",
        )
        # The list endpoint caches per coordinate; give each test its own
        # origin so cached rows from a sibling test cannot leak in.
        type(self)._origin_seq += 1
        self.lat = LAT + type(self)._origin_seq * 0.01
        self.lng = LNG
        cache.clear()

    def _restaurant(self, name, offset_km, max_radius_km=10.0):
        # ~110.6 km per degree of longitude at Dar es Salaam's latitude.
        lng = self.lng + offset_km / 110.6
        return Restaurant.objects.create(
            owner=self.owner,
            name=name,
            location="Test",
            geo_location=Point(lng, self.lat),
            max_delivery_radius_km=max_radius_km,
        )

    def _list(self, **params):
        response = self.client.get(
            "/api/restaurants/", {"lat": self.lat, "lng": self.lng, **params}
        )
        self.assertEqual(response.status_code, 200)
        data = response.data
        return data["results"] if isinstance(data, dict) else data

    def test_restaurants_are_returned_nearest_first(self):
        self._restaurant("Far", 4.0)
        self._restaurant("Near", 0.4)
        self._restaurant("Middle", 2.0)

        results = self._list()

        self.assertEqual([r["name"] for r in results], ["Near", "Middle", "Far"])
        distances = [r["distance_km"] for r in results]
        self.assertEqual(distances, sorted(distances))

    def test_search_radius_expands_for_sparse_areas(self):
        # Nothing inside 5 km, so the ring has to widen before returning.
        self._restaurant("Only Option", 8.0)

        results = self._list()

        self.assertEqual([r["name"] for r in results], ["Only Option"])

    def test_nearest_listing_is_returned_even_if_it_cannot_deliver(self):
        # 8 km away with a 1 km delivery radius: no ring ever satisfies the
        # delivery gate, so discovery falls back to the nearest listing.
        self._restaurant("Out Of Range", 8.0, max_radius_km=1.0)

        results = self._list()

        self.assertEqual([r["name"] for r in results], ["Out Of Range"])

    def test_dense_area_stays_in_the_nearby_ring(self):
        for i in range(6):
            self._restaurant(f"Near {i}", 0.2 + i * 0.1)
        self._restaurant("Distant", 40.0, max_radius_km=60.0)

        results = self._list(limit=6)

        names = [r["name"] for r in results]
        self.assertEqual(len(names), 6)
        self.assertNotIn("Distant", names)

    def test_ring_keeps_widening_until_it_can_fill_a_page(self):
        # Six nearby kitchens are not a full page, so discovery widens and
        # still lists the distant one rather than truncating the feed.
        for i in range(6):
            self._restaurant(f"Near {i}", 0.2 + i * 0.1)
        self._restaurant("Distant", 40.0, max_radius_km=60.0)

        results = self._list(limit=20)

        names = [r["name"] for r in results]
        self.assertEqual(len(names), 7)
        self.assertEqual(names[-1], "Distant")
        distances = [r["distance_km"] for r in results]
        self.assertEqual(distances, sorted(distances))
