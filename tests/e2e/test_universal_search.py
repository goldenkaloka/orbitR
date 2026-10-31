"""
Search must answer the word the customer typed.

Fuzzy matching is useful for typos, but a bare trigram cut-off matched
"wali" (rice) to "Ugali" because the two words share their tail, and the
customer got maize-meal dishes for a rice search.
"""

from decimal import Decimal

from apps.products.models import Food
from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from rest_framework.test import APIClient

POINT = Point(39.2083, -6.7924)


class UniversalSearchTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='owner_search', phone='+255700003100',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=self.owner,
            name='Search Diner',
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=20,
        )
        self.section = self.restaurant.sections.first()
        Food.objects.create(
            section=self.section, name='Wali Maharage', price=Decimal('3000.00'),
        )
        Food.objects.create(
            section=self.section, name='Ugali & Fish', price=Decimal('6000.00'),
        )
        Food.objects.create(
            section=self.section, name='Chapati', price=Decimal('1000.00'),
        )
        self.client = APIClient()

    def _food_names(self, query):
        response = self.client.get('/api/search/', {'q': query})
        self.assertEqual(response.status_code, 200)
        return [row['name'] for row in response.json()['products']]

    def test_shared_suffix_does_not_match(self):
        names = self._food_names('wali')
        self.assertIn('Wali Maharage', names)
        self.assertNotIn('Ugali & Fish', names)

    def test_typo_still_matches(self):
        self.assertIn('Ugali & Fish', self._food_names('ugaly'))

    def test_short_query_does_not_fuzzy_match(self):
        # "chai" scores 0.6 against "Chapati" on trigrams alone, but tea is
        # not chapati. Short queries only match on a real substring.
        self.assertNotIn('Chapati', self._food_names('chai'))

    def test_prefix_matches(self):
        self.assertIn('Ugali & Fish', self._food_names('uga'))

    def _restaurant(self, name, lng, lat):
        return Restaurant.objects.create(
            owner=self.owner,
            name=name,
            location='Test',
            geo_location=Point(lng, lat),
            average_prep_time=20,
        )

    def test_far_restaurant_is_still_searchable(self):
        # Search spans the catalogue, so a kitchen outside the customer's
        # neighbourhood is still findable by name.
        self._restaurant('Kigoma Grill', 29.6, -4.9)

        response = self.client.get('/api/search/', {
            'q': 'Kigoma', 'lat': -6.7924, 'lng': 39.2083,
        })

        self.assertEqual(response.status_code, 200)
        names = [row['name'] for row in response.json()['restaurants']]
        self.assertIn('Kigoma Grill', names)

    def test_food_from_far_restaurant_is_still_searchable(self):
        far = self._restaurant('Kigoma Grill', 29.6, -4.9)
        Food.objects.create(
            section=far.sections.first(), name='Kigoma Chips', price=Decimal('4000.00'),
        )

        response = self.client.get('/api/search/', {
            'q': 'Kigoma Chips', 'lat': -6.7924, 'lng': 39.2083,
        })

        names = [row['name'] for row in response.json()['products']]
        self.assertIn('Kigoma Chips', names)

    def test_equal_matches_are_ordered_nearest_first(self):
        self._restaurant('Kebab House', 39.25, -6.79)
        self._restaurant('Kebab House', 29.6, -4.9)

        response = self.client.get('/api/search/', {
            'q': 'Kebab', 'lat': -6.7924, 'lng': 39.2083,
        })

        distances = [row['distance_km'] for row in response.json()['restaurants']]
        self.assertEqual(len(distances), 2)
        self.assertEqual(distances, sorted(distances))
