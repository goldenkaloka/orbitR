from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.restaurants.models import MealPeriod, Restaurant
from apps.users.models import User
from datetime import time

LAT = -6.7924
LNG = 39.2083


@override_settings(
    CACHES={"default": {"django.core.cache.backends.locmem.LocMemCache"}}
)
class RestaurantDiscoveryTests(TestCase):
    """Customer feed: every active kitchen, nearest first.

    Location ranks the list but never filters it, so a customer in a quiet
    area still sees the whole catalogue instead of an empty feed.
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

    def _restaurant(self, name, offset_km, max_radius_km=10.0, pinned=True):
        # ~110.6 km per degree of longitude at Dar es Salaam's latitude.
        lng = self.lng + offset_km / 110.6
        return Restaurant.objects.create(
            owner=self.owner,
            name=name,
            location="Test",
            geo_location=Point(lng, self.lat) if pinned else None,
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

    def test_sparse_area_still_returns_the_only_kitchen(self):
        self._restaurant("Only Option", 8.0)

        results = self._list()

        self.assertEqual([r["name"] for r in results], ["Only Option"])

    def test_kitchen_outside_its_delivery_radius_is_still_listed(self):
        # 8 km away with a 1 km radius: the listing is shown anyway, because
        # the feed ranks by proximity and does not gate on the radius.
        self._restaurant("Out Of Range", 8.0, max_radius_km=1.0)

        results = self._list()

        self.assertEqual([r["name"] for r in results], ["Out Of Range"])

    def test_first_page_holds_only_the_nearest_kitchens(self):
        for i in range(6):
            self._restaurant(f"Near {i}", 0.2 + i * 0.1)
        self._restaurant("Distant", 40.0, max_radius_km=60.0)

        results = self._list(limit=6)

        names = [r["name"] for r in results]
        self.assertEqual(len(names), 6)
        self.assertNotIn("Distant", names)

    def test_every_active_kitchen_is_listed(self):
        for i in range(6):
            self._restaurant(f"Near {i}", 0.2 + i * 0.1)
        self._restaurant("Distant", 40.0, max_radius_km=60.0)

        results = self._list(limit=20)

        names = [r["name"] for r in results]
        self.assertEqual(len(names), 7)
        self.assertEqual(names[-1], "Distant")
        distances = [r["distance_km"] for r in results]
        self.assertEqual(distances, sorted(distances))

    def test_unpinned_kitchen_is_listed_last_without_a_distance(self):
        self._restaurant("Pinned", 0.5)
        self._restaurant("Unpinned", 0.5, pinned=False)

        results = self._list()

        self.assertEqual([r["name"] for r in results], ["Pinned", "Unpinned"])
        self.assertIsNone(results[-1]["distance_km"])
        self.assertIsNotNone(results[0]["distance_km"])

    def test_feed_reports_whether_a_kitchen_takes_preorders(self):
        takes = self._restaurant("Takes Preorders", 0.4)
        takes.preorder_slot_capacity = 4
        takes.preorders_enabled = True
        takes.save(
            update_fields=["preorder_slot_capacity", "preorders_enabled"],
        )
        MealPeriod.objects.create(
            restaurant=takes,
            kind=MealPeriod.LUNCH,
            start_time=time(11, 0),
            end_time=time(15, 0),
        )
        closed = self._restaurant("No Preorders", 0.6)
        closed.preorder_slot_capacity = 0
        closed.save(update_fields=["preorder_slot_capacity"])

        results = {r["name"]: r["preorder"] for r in self._list()}

        self.assertIsNone(results["No Preorders"])
        hint = results["Takes Preorders"]
        self.assertIsNotNone(hint)
        self.assertTrue(hint["accepting"])
        self.assertEqual(hint["meal"], "Lunch")
        self.assertIn(hint["day"], ("Today", "Tomorrow"))
