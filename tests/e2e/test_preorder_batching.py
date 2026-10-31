"""
Pre-order slot batching.

Every pre-order for the same restaurant and eating-time slot must land in one
batch, so the kitchen sees the slot as a single unit of work ahead of dispatch.
Live orders keep their own accumulate/soft-lock flow and never eat into a slot
batch.
"""

from datetime import timedelta
from decimal import Decimal

from apps.core.geo_utils import latlng_to_h3
from apps.orders.models import DeliveryBatch, Order
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from domain.services.batch_pipeline import BatchPipeline
from domain.services.orders.scheduled_orders import release_at_for, slot_window_for

POINT = Point(39.2083, -6.7924)


class PreorderBatchingTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='cust_batch', phone='+255700001020',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='owner_batch', phone='+255700001021',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Batch Diner',
            preorders_enabled=True,
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=25,
        )
        section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        Food.objects.create(
            section=section, name='Slot Plate', price=Decimal('10000.00'),
        )

    def _slot(self, offset_slots=0):
        base = (timezone.now() + timedelta(hours=2)).replace(
            second=0, microsecond=0,
        )
        base = base.replace(minute=(base.minute // 15) * 15)
        return base + timedelta(minutes=15 * offset_slots)

    def _order(self, scheduled_for):
        return Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='CONFIRMED',
            is_paid=True,
            h3_index=latlng_to_h3(POINT.y, POINT.x),
            items_subtotal=Decimal('10000.00'),
            delivery_fee=Decimal('1500.00'),
            service_fee=Decimal('500.00'),
            total_amount=Decimal('12000.00'),
            scheduled_for=scheduled_for,
            release_at=(
                release_at_for(self.restaurant, scheduled_for)
                if scheduled_for else None
            ),
        )

    def test_same_slot_preorders_share_one_batch(self):
        slot = self._slot()
        first = self._order(slot + timedelta(minutes=2))
        second = self._order(slot + timedelta(minutes=13))

        batch_one = BatchPipeline.on_order_confirmed(first)
        batch_two = BatchPipeline.on_order_confirmed(second)

        self.assertIsNotNone(batch_one)
        self.assertEqual(batch_one.id, batch_two.id)
        self.assertEqual(batch_one.orders.count(), 2)
        self.assertEqual(batch_one.preorder_slot_start, slot)

    def test_different_slots_get_different_batches(self):
        noon = self._slot()
        later = self._slot(offset_slots=1)

        first = BatchPipeline.on_order_confirmed(
            self._order(noon + timedelta(minutes=1)),
        )
        second = BatchPipeline.on_order_confirmed(
            self._order(later + timedelta(minutes=1)),
        )

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.preorder_slot_start, noon)
        self.assertEqual(second.preorder_slot_start, later)

    def test_live_orders_do_not_join_a_slot_batch(self):
        slot = self._slot()
        preorder_batch = BatchPipeline.on_order_confirmed(
            self._order(slot + timedelta(minutes=5)),
        )

        live_order = self._order(None)
        live_batch = BatchPipeline.on_order_confirmed(live_order)

        self.assertNotEqual(preorder_batch.id, live_batch.id)
        self.assertIsNone(live_batch.preorder_slot_start)
        self.assertEqual(preorder_batch.orders.count(), 1)

    def test_batch_stays_open_until_the_slots_last_release(self):
        slot = self._slot()
        batch = BatchPipeline.on_order_confirmed(
            self._order(slot + timedelta(minutes=1)),
        )

        _, slot_end = slot_window_for(slot + timedelta(minutes=1))
        last_release = release_at_for(self.restaurant, slot_end)
        deadline = batch.accumulating_since + timedelta(
            minutes=batch.accumulate_window_minutes,
        )

        self.assertGreaterEqual(deadline, last_release)

    def test_busy_restaurant_window_does_not_close_a_slot_batch(self):
        slot = self._slot()
        preorder_batch = BatchPipeline.on_order_confirmed(
            self._order(slot + timedelta(minutes=1)),
        )
        six_minutes_ago = timezone.now() - timedelta(minutes=6)
        DeliveryBatch.objects.filter(id=preorder_batch.id).update(
            accumulating_since=six_minutes_ago,
        )
        for _ in range(2):
            DeliveryBatch.objects.create(
                restaurant=self.restaurant,
                status='ACCUMULATING',
                batch_type='SINGLE_RESTAURANT',
                accumulating_since=six_minutes_ago,
            )

        BatchPipeline.close_due_accumulating_batches()

        preorder_batch.refresh_from_db()
        self.assertEqual(preorder_batch.status, 'ACCUMULATING')
        self.assertEqual(
            DeliveryBatch.objects.filter(
                restaurant=self.restaurant,
                status='SOFT_LOCK',
                preorder_slot_start__isnull=True,
            ).count(),
            2,
        )

    def test_slot_batch_closes_when_its_own_window_expires(self):
        slot = self._slot()
        batch = BatchPipeline.on_order_confirmed(
            self._order(slot + timedelta(minutes=1)),
        )
        DeliveryBatch.objects.filter(id=batch.id).update(
            accumulating_since=timezone.now()
            - timedelta(minutes=batch.accumulate_window_minutes + 1),
        )

        BatchPipeline.close_due_accumulating_batches()

        batch.refresh_from_db()
        self.assertEqual(batch.status, 'SOFT_LOCK')
