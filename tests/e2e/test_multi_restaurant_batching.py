"""
Multi-restaurant batching.

Platform-managed restaurants may pool orders into a shared batch. Partner
restaurants must never be pooled — that would expose one restaurant's orders to
another and drag their own driver pool into a run that includes competitors.
"""

from decimal import Decimal

from apps.delivery.models import Driver
from apps.orders.models import BatchRestaurant, DeliveryBatch, Order
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from rest_framework.test import APIClient
from django.utils import timezone

POINT = Point(39.2083, -6.7924)


class MultiRestaurantBatchingTests(TestCase):
    def setUp(self):
        # force_authenticate() lives on DRF's client, not Django's.
        self.client = APIClient()
        self.platform_owner_a = User.objects.create_user(
            username='platA', phone='+255700000031',
            password='password123', role='vendor',
        )
        self.platform_owner_b = User.objects.create_user(
            username='platB', phone='+255700000032',
            password='password123', role='vendor',
        )
        self.partner_owner = User.objects.create_user(
            username='partner', phone='+255700000033',
            password='password123', role='vendor',
        )
        self.customer = User.objects.create_user(
            username='cust', phone='+255700000034',
            password='password123', role='customer',
        )
        self.admin = User.objects.create_user(
            username='adm', phone='+255700000035',
            password='password123', role='admin',
        )

        self.platform_a = self._restaurant('Platform A', self.platform_owner_a, managed=True)
        self.platform_b = self._restaurant('Platform B', self.platform_owner_b, managed=True)
        self.partner = self._restaurant('Partner Cafe', self.partner_owner, managed=False)

    def _restaurant(self, name, owner, managed):
        restaurant = Restaurant.objects.create(
            owner=owner,
            name=name,
            location=name,
            geo_location=POINT,
            source_type=(
                Restaurant.SOURCE_PLATFORM_MANAGED if managed
                else Restaurant.SOURCE_PARTNER
            ),
            is_verified_partner=not managed,
            commission_rate=Decimal('0.050'),
        )
        section = RestaurantSection.objects.create(restaurant=restaurant, name='Main')
        Food.objects.create(
            section=section, name=f'{name} item', price=Decimal('10000.00'),
        )
        return restaurant

    def _order(self, restaurant, suffix='a'):
        from apps.core.geo_utils import latlng_to_h3

        return Order.objects.create(
            customer=self.customer,
            restaurant=restaurant,
            status='CONFIRMED',
            # PlaceOrderService always stamps an h3 cell. Without it,
            # on_order_confirmed short-circuits to _create_single_batch and
            # never reaches _find_compatible_batch, so pooling is never tested.
            h3_index=latlng_to_h3(POINT.y, POINT.x),
            items_subtotal=Decimal('10000.00'),
            delivery_fee=Decimal('1500.00'),
            service_fee=Decimal('500.00'),
            total_amount=Decimal('12000.00'),
            notes=suffix,
        )

    def _batch(self, order):
        from domain.services.batch_pipeline import BatchPipeline
        return BatchPipeline.on_order_confirmed(order)

    def _to_pending_driver(self, batch):
        """Advance past ACCUMULATING so the batch is dispatchable."""
        batch.status = 'PENDING_DRIVER'
        batch.waiting_since = timezone.now()
        batch.save(update_fields=['status', 'waiting_since'])
        batch.section_statuses.update(status='READY')
        return batch

    # ── Eligibility gate ──────────────────────────────────────────────────────

    def test_only_platform_managed_restaurants_may_pool(self):
        self.assertTrue(self.platform_a.allows_multi_restaurant_batching)
        self.assertTrue(self.platform_b.allows_multi_restaurant_batching)
        self.assertFalse(self.partner.allows_multi_restaurant_batching)

    # ── Pooling behaviour ─────────────────────────────────────────────────────

    def test_two_platform_restaurants_share_one_batch(self):
        first = self._order(self.platform_a)
        second = self._order(self.platform_b)

        batch_one = self._batch(first)
        batch_two = self._batch(second)

        self.assertIsNotNone(batch_one)
        self.assertEqual(batch_one.id, batch_two.id)
        self.assertEqual(batch_one.orders.count(), 2)
        self.assertEqual(batch_one.batch_type, 'MULTI_RESTAURANT')

    def test_partner_orders_never_pool_with_each_other(self):
        """Each partner restaurant keeps its own batch."""
        other_partner = self._restaurant('Partner Two', self.partner_owner, managed=False)

        first = self._order(self.partner)
        second = self._order(other_partner)

        batch_one = self._batch(first)
        batch_two = self._batch(second)

        self.assertNotEqual(batch_one.id, batch_two.id)
        self.assertEqual(batch_one.batch_type, 'SINGLE_RESTAURANT')
        self.assertEqual(batch_two.batch_type, 'SINGLE_RESTAURANT')

    def test_partner_order_does_not_join_a_platform_batch(self):
        platform_order = self._order(self.platform_a)
        partner_order = self._order(self.partner)

        platform_batch = self._batch(platform_order)
        partner_batch = self._batch(partner_order)

        self.assertNotEqual(platform_batch.id, partner_batch.id)
        self.assertEqual(partner_batch.batch_type, 'SINGLE_RESTAURANT')
        self.assertEqual(platform_batch.orders.count(), 1)

    def test_platform_order_does_not_join_a_partner_batch(self):
        """The other direction — a pooled order must not land in a partner run."""
        partner_order = self._order(self.partner)
        platform_order = self._order(self.platform_a)

        partner_batch = self._batch(partner_order)
        platform_batch = self._batch(platform_order)

        self.assertNotEqual(partner_batch.id, platform_batch.id)
        self.assertEqual(partner_batch.orders.count(), 1)

    # ── BatchRestaurant bookkeeping ───────────────────────────────────────────

    def test_batch_restaurant_rows_carry_pickup_sequence(self):
        first = self._order(self.platform_a)
        self._batch(first)

        second = self._order(self.platform_b)
        batch = self._batch(second)

        rows = list(
            BatchRestaurant.objects.filter(batch=batch).order_by('pickup_sequence')
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [r.pickup_sequence for r in rows], [1, 2],
        )
        self.assertEqual(rows[0].restaurant_id, self.platform_a.id)

    def test_sync_drops_restaurants_with_no_orders(self):
        first = self._order(self.platform_a)
        second = self._order(self.platform_b)
        batch = self._batch(first)
        self._batch(second)

        self.assertEqual(BatchRestaurant.objects.filter(batch=batch).count(), 2)

        # If the second restaurant's order leaves the batch, so does its row.
        second.batch = None
        second.save(update_fields=['batch'])

        from domain.services.batch_pipeline import BatchPipeline
        BatchPipeline._sync_batch_restaurants(batch)

        rows = BatchRestaurant.objects.filter(batch=batch)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().restaurant_id, self.platform_a.id)
        batch.refresh_from_db()
        self.assertEqual(batch.batch_type, 'SINGLE_RESTAURANT')

    # ── Driver eligibility ────────────────────────────────────────────────────

    def _driver(self, username, phone, driver_type='platform', home=None):
        user = User.objects.create_user(
            username=username, phone=phone,
            password='password123', role='driver',
        )
        return Driver.objects.create(
            user=user, status='AVAILABLE', driver_type=driver_type,
            home_restaurant=home, location=POINT, max_active_orders=10,
        )

    def _mark_sections_ready(self, batch):
        batch.section_statuses.update(status='READY')

    def test_platform_driver_can_take_a_pooled_batch(self):
        from domain.services.orders.order_assignment import AssignBatchService

        first = self._order(self.platform_a)
        second = self._order(self.platform_b)
        batch = self._batch(first)
        self._batch(second)
        self._to_pending_driver(batch)

        driver = self._driver('plat_driver', '+255700000036')
        assigned = AssignBatchService.execute(str(batch.id), str(driver.id), self.admin)

        self.assertEqual(assigned.orders.count(), 2)
        self.assertEqual(assigned.status, 'DISPATCHED')

    def test_restaurant_driver_cannot_take_a_pooled_batch(self):
        """
        A restaurant's own driver must not run a batch that includes a
        competitor's order.
        """
        from domain.services.orders.order_assignment import AssignBatchService

        first = self._order(self.platform_a)
        second = self._order(self.platform_b)
        batch = self._batch(first)
        self._batch(second)
        self._to_pending_driver(batch)

        own_driver = self._driver(
            'own_driver', '+255700000037',
            driver_type='restaurant', home=self.platform_a,
        )

        with self.assertRaises(PermissionError):
            AssignBatchService.execute(
                str(batch.id), str(own_driver.id), self.admin,
            )

    def test_restaurant_driver_can_still_take_its_own_single_batch(self):
        from domain.services.orders.order_assignment import AssignBatchService

        order = self._order(self.platform_a)
        batch = self._batch(order)
        self._to_pending_driver(batch)

        own_driver = self._driver(
            'solo_driver', '+255700000038',
            driver_type='restaurant', home=self.platform_a,
        )
        assigned = AssignBatchService.execute(
            str(batch.id), str(own_driver.id), self.admin,
        )
        self.assertEqual(assigned.driver_id, own_driver.id)

    # ── Cross-restaurant data isolation ───────────────────────────────────────

    def test_vendor_cannot_see_another_restaurants_orders_in_a_pooled_batch(self):
        first = self._order(self.platform_a)
        second = self._order(self.platform_b)
        batch = self._batch(first)
        self._batch(second)

        self.client.force_authenticate(self.platform_owner_a)
        response = self.client.get('/api/batches/')

        self.assertEqual(response.status_code, 200)
        # The endpoint is paginated (LimitOffsetPagination), so rows live
        # under 'results' rather than at the top level.
        returned_ids = {row['id'] for row in response.json()['results']}

        # The vendor still sees their own order...
        self.assertIn(str(first.id), returned_ids)
        # ...but never the other restaurant's.
        self.assertNotIn(str(second.id), returned_ids)

    def test_vendor_cannot_allocate_a_pooled_batch(self):
        first = self._order(self.platform_a)
        second = self._order(self.platform_b)
        batch = self._batch(first)
        self._batch(second)
        self._to_pending_driver(batch)

        # Even in restaurant-dispatch mode, a pooled batch is OrbitR's.
        self.platform_a.dispatch_mode = Restaurant.DISPATCH_RESTRANT
        self.platform_b.dispatch_mode = Restaurant.DISPATCH_RESTRANT
        self.platform_a.save(update_fields=['dispatch_mode'])
        self.platform_b.save(update_fields=['dispatch_mode'])

        driver = self._driver('pool_driver', '+255700000039')

        self.client.force_authenticate(self.platform_owner_a)
        response = self.client.post(
            f'/api/batches/{batch.id}/assign-driver/',
            {'driver_id': str(driver.id)},
            format='json',
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_can_allocate_a_pooled_batch_through_the_endpoint(self):
        first = self._order(self.platform_a)
        second = self._order(self.platform_b)
        batch = self._batch(first)
        self._batch(second)
        self._to_pending_driver(batch)

        driver = self._driver('admin_assigned', '+255700000040')

        self.client.force_authenticate(self.admin)
        response = self.client.post(
            f'/api/batches/{batch.id}/assign-driver/',
            {'driver_id': str(driver.id)},
            format='json',
        )
        self.assertEqual(response.status_code, 200)

    # ── Queue visibility ──────────────────────────────────────────────────────

    def test_admin_queue_reports_restaurants_in_pickup_order(self):
        first = self._order(self.platform_a)
        second = self._order(self.platform_b)
        batch = self._batch(first)
        self._batch(second)
        self._to_pending_driver(batch)

        self.client.force_authenticate(self.admin)
        response = self.client.get('/api/admin/pending-batches/')

        self.assertEqual(response.status_code, 200)
        batch_row = next(
            row for row in response.json()['batches'] if row['batch_type'] == 'MULTI_RESTAURANT'
        )
        self.assertEqual(batch_row['restaurant_count'], 2)
        self.assertEqual(
            [r['name'] for r in batch_row['restaurants']],
            ['Platform A', 'Platform B'],
        )

    def test_eligible_drivers_marks_affiliated_drivers_unservable(self):
        first = self._order(self.platform_a)
        second = self._order(self.platform_b)
        batch = self._batch(first)
        self._batch(second)

        platform_driver = self._driver('union_platform', '+255700000041')
        affiliated = self._driver(
            'union_affiliated', '+255700000042',
            driver_type='restaurant', home=self.platform_a,
        )

        self.client.force_authenticate(self.admin)
        response = self.client.get(
            f'/api/admin/batches/{batch.id}/eligible-drivers/'
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['is_multi_restaurant'])

        rows = {row['id']: row for row in body['drivers']}
        self.assertTrue(rows[str(platform_driver.id)]['can_serve'])
        self.assertFalse(rows[str(affiliated.id)]['can_serve'])

        # Unservable drivers are sorted below servable ones.
        servable_flags = [row['can_serve'] for row in body['drivers']]
        self.assertEqual(servable_flags, sorted(servable_flags, reverse=True))
