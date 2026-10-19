"""
Public marketplace catalogue.

The storefront browses anonymously, so the read path must:
  - never 500 on AnonymousUser (Q(seller=AnonymousUser) used to raise)
  - never leak a non-ACTIVE listing to an anonymous visitor
  - never expose a write to an anonymous visitor
  - keep search and sort out of each other's cached results
"""

from decimal import Decimal

from apps.marketplace.models import MarketplaceCategory, MarketplaceProduct
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

LIST_URL = '/api/marketplace/products/'

# The view caches list responses for 900s in Redis. Tests get their own
# in-process cache so a previous run can never bleed rows into these.
TEST_CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'marketplace-catalogue-tests',
    }
}


@override_settings(CACHES=TEST_CACHES)
class PublicCatalogueTests(TestCase):
    def setUp(self):
        cache.clear()

        # force_authenticate() lives on DRF's client, not Django's.
        self.client = APIClient()

        self.category = MarketplaceCategory.objects.create(
            name='Elektroniki', icon='cpu',
        )

        self.owner = User.objects.create_user(
            username='seller', phone='+255700000051',
            password='password123', role='vendor',
        )
        self.buyer = User.objects.create_user(
            username='buyer', phone='+255700000052',
            password='password123', role='customer',
        )
        self.stranger = User.objects.create_user(
            username='stranger', phone='+255700000053',
            password='password123', role='customer',
        )

        self.active = self._product('Laptop Pro 14', 'A fast machine', 500000)
        self.expired = self._product(
            'Old Radio', 'No longer available', 50000, status='EXPIRED',
        )
        self.pending = self._product(
            'Pending Item', 'Awaiting fee', 75000, status='PENDING_PAYMENT',
        )

    def _product(self, name, description, price, status='ACTIVE'):
        return MarketplaceProduct.objects.create(
            seller=self.owner,
            category=self.category,
            name=name,
            description=description,
            price=Decimal(str(price)),
            whatsapp_number='+255700000051',
            location=Point(39.2083, -6.7924),
            status=status,
            is_fee_paid=True,
        )

    # ── Public reads ─────────────────────────────────────────────────────────

    def test_anonymous_can_list(self):
        response = self.client.get(LIST_URL)

        self.assertEqual(response.status_code, 200)
        ids = {row['id'] for row in response.json()['results']}
        self.assertIn(str(self.active.id), ids)

    def test_anonymous_list_only_shows_active(self):
        response = self.client.get(LIST_URL)
        ids = {row['id'] for row in response.json()['results']}

        self.assertNotIn(str(self.expired.id), ids)
        self.assertNotIn(str(self.pending.id), ids)

    def test_anonymous_retrieve_of_active_succeeds(self):
        response = self.client.get(f'{LIST_URL}{self.active.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['id'], str(self.active.id))

    def test_anonymous_retrieve_of_non_active_is_404_not_500(self):
        """
        Regression: Q(seller=AnonymousUser) raised
        "Field 'id' expected a number but got AnonymousUser" — every public
        product page 500'd. Must be a clean 404.
        """
        for product in (self.expired, self.pending):
            with self.subTest(status=product.status):
                response = self.client.get(f'{LIST_URL}{product.id}/')
                self.assertEqual(response.status_code, 404)

    def test_anonymous_cannot_see_a_pending_listing_of_another_seller(self):
        self.pending.status = 'PENDING_REVIEW'
        self.pending.save(update_fields=['status'])

        response = self.client.get(f'{LIST_URL}{self.pending.id}/')
        self.assertEqual(response.status_code, 404)

    def test_authenticated_owner_still_sees_own_non_active_listing(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(f'{LIST_URL}{self.expired.id}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['id'], str(self.expired.id))

    def test_authenticated_non_owner_cannot_see_non_active_listing(self):
        self.client.force_authenticate(self.stranger)
        response = self.client.get(f'{LIST_URL}{self.expired.id}/')
        self.assertEqual(response.status_code, 404)

    # ── Writes stay locked ───────────────────────────────────────────────────

    def test_anonymous_cannot_create(self):
        response = self.client.post(
            LIST_URL,
            {
                'name': 'Sneaky', 'description': 'x', 'price': '1000',
                'whatsapp_number': '+255700000051',
                'location': {'latitude': -6.7924, 'longitude': 39.2083},
            },
            format='json',
        )
        self.assertIn(response.status_code, (401, 403))

    def test_anonymous_cannot_list_own_listings(self):
        response = self.client.get(f'{LIST_URL}mine/')
        self.assertIn(response.status_code, (401, 403))

    # ── Search ───────────────────────────────────────────────────────────────

    def test_search_matches_name(self):
        response = self.client.get(f'{LIST_URL}?q=Laptop')
        ids = {row['id'] for row in response.json()['results']}

        self.assertEqual(ids, {str(self.active.id)})

    def test_search_matches_description(self):
        extra = self._product('Misc', 'A fast machine in mint condition', 1000)
        response = self.client.get(f'{LIST_URL}?q=mint')
        ids = {row['id'] for row in response.json()['results']}

        self.assertIn(str(extra.id), ids)

    def test_search_is_case_insensitive(self):
        response = self.client.get(f'{LIST_URL}?q=lApToP')
        ids = {row['id'] for row in response.json()['results']}
        self.assertEqual(ids, {str(self.active.id)})

    def test_search_with_no_match_returns_empty(self):
        response = self.client.get(f'{LIST_URL}?q=zzzznotathing')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['results'], [])

    # ── Sort ─────────────────────────────────────────────────────────────────

    def _prices(self, response):
        return [Decimal(row['price']) for row in response.json()['results']]

    def test_sort_price_ascending(self):
        self._product('Cheap', 'cheap thing', 1000)
        response = self.client.get(f'{LIST_URL}?sort=price_asc')
        prices = self._prices(response)

        self.assertEqual(prices, sorted(prices))

    def test_sort_price_descending(self):
        self._product('Cheap', 'cheap thing', 1000)
        response = self.client.get(f'{LIST_URL}?sort=price_desc')
        prices = self._prices(response)

        self.assertEqual(prices, sorted(prices, reverse=True))

    def test_default_order_is_newest_first(self):
        response = self.client.get(LIST_URL)
        names = [row['name'] for row in response.json()['results']]

        self.assertEqual(names[0], 'Laptop Pro 14')

    def test_nearest_sort_without_coordinates_does_not_break(self):
        """`nearest` with no lat/lng must still paginate deterministically."""
        response = self.client.get(f'{LIST_URL}?sort=nearest')
        self.assertEqual(response.status_code, 200)

        first = [r['id'] for r in response.json()['results']]
        second = [r['id'] for r in
                  self.client.get(f'{LIST_URL}?sort=nearest').json()['results']]
        self.assertEqual(first, second)

    def test_unknown_sort_falls_back_without_error(self):
        response = self.client.get(f'{LIST_URL}?sort=bogus')
        self.assertEqual(response.status_code, 200)

    # ── Cache isolation ──────────────────────────────────────────────────────

    def test_search_results_are_not_served_from_another_searchs_cache(self):
        """
        Regression: q and sort were absent from the Redis key, so searching
        "Laptop" then "Radio" returned the first search's cached rows.
        """
        radio = self._product('Radio Deluxe', 'tuner', 12000)

        first = self.client.get(f'{LIST_URL}?q=Laptop')
        self.assertEqual(
            {r['id'] for r in first.json()['results']}, {str(self.active.id)},
        )

        second = self.client.get(f'{LIST_URL}?q=Radio')
        self.assertEqual(
            {r['id'] for r in second.json()['results']}, {str(radio.id)},
        )

    def test_sort_order_is_not_served_from_another_sorts_cache(self):
        self._product('Cheap', 'cheap thing', 1000)

        ascending = self.client.get(f'{LIST_URL}?sort=price_asc').json()['results']
        descending = self.client.get(f'{LIST_URL}?sort=price_desc').json()['results']

        self.assertNotEqual(
            [r['id'] for r in ascending], [r['id'] for r in descending],
        )