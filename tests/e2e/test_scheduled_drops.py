"""Scheduled drops: many orders, one trip, one gate.

Covers the commercial promises of the feature:
* a drop order pays the window's flat fee, never distance pricing;
* two drop windows at the same time are two trips, not one batch;
* a drop trip pays the flat trip fee even when the fees collected fall short;
* capacity and cut-off are enforced at booking time;
* a bucket pointed at a drop charges each member the flat fee;
* the below-minimum policy runs, rolls or cancels as configured.
"""

from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from apps.orders.models import DeliveryBatch, DropPoint, DropWindow, Order, OrderPool, PoolMember
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.conf import settings
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from domain.services.batch_pipeline import BatchPipeline
from domain.services.orders.drop_service import (
    apply_low_fill_policy,
    describe_window,
    validate_drop_booking,
    DropUnavailable,
)
from domain.services.orders.order_placement import PlaceOrderService
from domain.services.orders.order_pooling import PoolOrderService
from domain.services.orders.scheduled_orders import slot_window_for
from domain.services.pricing_service import PricingService

POINT = Point(39.2083, -6.7924)
TZ = ZoneInfo(getattr(settings, 'PREORDER_TIMEZONE', 'Africa/Dar_es_Salaam'))


def _booking_start(days=2, hour=12):
    """A deterministic future window start in the market's clock."""
    local = timezone.now().astimezone(TZ) + timedelta(days=days)
    return local.replace(hour=hour, minute=0, second=0, microsecond=0)


class DropTestBase(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='drop_customer', phone='+255700010001',
            password='password123', role='customer',
        )
        self.other = User.objects.create_user(
            username='drop_friend', phone='+255700010002',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='drop_owner', phone='+255700010003',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Drop Diner',
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=25,
        )
        self.section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        self.food = Food.objects.create(
            section=self.section, name='Drop Plate', price=Decimal('10000.00'),
        )
        self.point = DropPoint.objects.create(
            name='Kijichi Hostel Gate',
            description='Behind the kiosk',
            geo_location=Point(39.2110, -6.7950),
            min_orders=3,
            max_orders=8,
        )

    def _window(self, point=None, start=None, fee='500.00', max_orders=8,
                weekday=None, min_orders=None):
        start = start or _booking_start()
        point = point or self.point
        if min_orders is not None:
            point.min_orders = min_orders
            point.save(update_fields=['min_orders'])
        return DropWindow.objects.create(
            drop_point=point,
            label='Lunch',
            weekday=start.weekday() if weekday is None else weekday,
            start_time=start.time().replace(second=0, microsecond=0),
            end_time=(start + timedelta(hours=1)).time().replace(second=0, microsecond=0),
            cutoff_time=(start - timedelta(minutes=30)).time().replace(second=0, microsecond=0),
            delivery_fee=Decimal(fee),
            max_orders=max_orders,
        )

    def _place(self, window, customer=None, scheduled_for=None):
        return PlaceOrderService.execute({
            'customer': customer or self.customer,
            'restaurant': self.restaurant,
            'fulfillment_type': Order.FULFILLMENT_DELIVERY,
            'drop_window': window,
            'scheduled_for': scheduled_for,
            'items': [{'food': self.food, 'quantity': 1}],
        })

    def _direct_order(self, window, scheduled_for, customer=None, status='CONFIRMED'):
        start = scheduled_for or _booking_start()
        return Order.objects.create(
            customer=customer or self.customer,
            restaurant=self.restaurant,
            status=status,
            is_paid=True,
            drop_window=window,
            drop_point=window.drop_point,
            scheduled_for=start,
            delivery_fee=window.delivery_fee,
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('10000.00'),
            total_amount=Decimal('10800.00'),
        )


class DropPlacementTests(DropTestBase):
    def test_drop_order_pays_the_flat_fee_not_distance(self):
        start = _booking_start()
        window = self._window(start=start)

        order = self._place(window, scheduled_for=start)

        self.assertEqual(order.delivery_fee, Decimal('500.00'))
        self.assertEqual(order.drop_window_id, window.id)
        self.assertEqual(order.drop_point_id, self.point.id)
        self.assertEqual(order.scheduled_for, start)
        self.assertEqual(order.delivery_location.x, self.point.geo_location.x)
        self.assertEqual(order.delivery_location.y, self.point.geo_location.y)
        self.assertIsNotNone(order.release_at)

    def test_capacity_refuses_the_next_order(self):
        start = _booking_start()
        window = self._window(start=start, max_orders=2)

        self._direct_order(window, start, customer=self.customer)
        self._direct_order(window, start, customer=self.other)

        with self.assertRaisesMessage(ValueError, 'full'):
            self._place(window, scheduled_for=start)

    def test_order_after_cutoff_is_refused(self):
        start = _booking_start()
        window = self._window(start=start)
        closed_at = start + timedelta(minutes=5)

        with self.assertRaises(DropUnavailable):
            validate_drop_booking(window, start, now=closed_at)

    def test_inactive_point_is_refused(self):
        start = _booking_start()
        window = self._window(start=start)
        self.point.is_active = False
        self.point.save(update_fields=['is_active'])

        with self.assertRaises(DropUnavailable):
            validate_drop_booking(window, start)

    def test_second_kitchen_cannot_take_the_same_window(self):
        start = _booking_start()
        window = self._window(start=start)
        self._place(window, scheduled_for=start)

        other_owner = User.objects.create_user(
            username='drop_owner_two', phone='+255700010004',
            password='password123', role='vendor',
        )
        second = Restaurant.objects.create(
            owner=other_owner,
            name='Second Diner',
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=25,
        )
        second_section = RestaurantSection.objects.create(
            restaurant=second, name='Main',
        )
        other_food = Food.objects.create(
            section=second_section, name='Other Plate', price=Decimal('9000.00'),
        )

        with self.assertRaisesMessage(DropUnavailable, 'served by'):
            PlaceOrderService.execute({
                'customer': self.other,
                'restaurant': second,
                'fulfillment_type': Order.FULFILLMENT_DELIVERY,
                'drop_window': window,
                'scheduled_for': start,
                'items': [{'food': other_food, 'quantity': 1}],
            })


class DropBatchingTests(DropTestBase):
    def test_two_drop_windows_at_the_same_time_are_two_batches(self):
        start = _booking_start()
        second_point = DropPoint.objects.create(
            name='Mikocheni Campus Block',
            geo_location=Point(39.2130, -6.7970),
        )
        first_window = self._window(start=start)
        second_window = self._window(point=second_point, start=start)

        first = self._direct_order(first_window, start, customer=self.customer)
        second = self._direct_order(second_window, start, customer=self.other)

        first_batch = BatchPipeline.on_order_confirmed(first)
        second_batch = BatchPipeline.on_order_confirmed(second)

        self.assertNotEqual(first_batch.id, second_batch.id)
        self.assertEqual(first_batch.drop_window_id, first_window.id)
        self.assertEqual(second_batch.drop_window_id, second_window.id)
        self.assertEqual(first_batch.preorder_slot_start, second_batch.preorder_slot_start)

    def test_orders_for_one_drop_window_share_a_batch(self):
        start = _booking_start()
        window = self._window(start=start)
        first = self._direct_order(window, start, customer=self.customer)
        second = self._direct_order(window, start, customer=self.other)

        first_batch = BatchPipeline.on_order_confirmed(first)
        second_batch = BatchPipeline.on_order_confirmed(second)

        self.assertEqual(first_batch.id, second_batch.id)
        self.assertEqual(first_batch.orders.count(), 2)
        self.assertEqual(first_batch.drop_window_id, window.id)

    def test_drop_batch_pays_the_flat_trip_fee_uncapped(self):
        start = _booking_start()
        window = self._window(start=start)
        orders = [
            self._direct_order(window, start, customer=self.customer),
            self._direct_order(window, start, customer=self.other),
        ]
        pricing = {
            'original_total': Decimal('1000.00'),
            'discounted_total': Decimal('1000.00'),
        }

        payout, collected = BatchPipeline._calculate_driver_payout(
            orders, route_dict=None, pricing=pricing,
        )

        self.assertEqual(payout, Decimal('3000.00'))
        self.assertEqual(collected, Decimal('1600.00'))
        self.assertGreater(payout, collected)


class DropBucketTests(DropTestBase):
    def test_bucket_charges_each_member_the_flat_fee(self):
        start = _booking_start()
        window = self._window(start=start)

        pool = PoolOrderService.create_pool(
            initiator=self.customer,
            restaurant_id=self.restaurant.id,
            delivery_address=None,
            delivery_location=None,
            drop_window=window,
        )
        self.assertEqual(pool.drop_window_id, window.id)
        self.assertEqual(pool.scheduled_for, start)
        self.assertEqual(pool.delivery_location.x, self.point.geo_location.x)

        member_one = pool.members.get(user=self.customer)
        member_one.items_subtotal = Decimal('10000.00')
        member_one.save(update_fields=['items_subtotal'])

        PoolMember.objects.create(
            pool=pool, user=self.other, status='ADDING',
            items_subtotal=Decimal('20000.00'),
        )

        PoolOrderService._refresh_fee_preview(pool)
        pool.refresh_from_db()

        self.assertEqual(pool.total_delivery_fee, Decimal('1000.00'))
        shares = list(pool.members.values_list('delivery_fee_share', flat=True))
        self.assertEqual(shares, [Decimal('500.00'), Decimal('500.00')])


class DropLowFillTests(DropTestBase):
    def _set_policy(self, action):
        from apps.pricing.models import PricingPolicy
        from django.core.cache import cache

        policy, _ = PricingPolicy.objects.get_or_create(
            name='Default Policy', defaults={'is_active': True},
        )
        policy.is_active = True
        policy.drop_low_fill_policy = action
        policy.save(update_fields=['is_active', 'drop_low_fill_policy'])
        cache.delete(PricingService.CACHE_KEY_ACTIVE_POLICY)
        return policy

    def test_run_policy_keeps_the_trip(self):
        start = _booking_start()
        window = self._window(start=start, min_orders=3)
        self._set_policy('RUN')
        order = self._direct_order(window, start)

        outcome = apply_low_fill_policy(window, start.date())

        self.assertEqual(outcome, 'RUN')
        order.refresh_from_db()
        self.assertEqual(order.status, 'CONFIRMED')

    def test_roll_policy_moves_orders_to_the_next_window(self):
        start = _booking_start()
        window = self._window(start=start, min_orders=3)
        self._set_policy('ROLL')
        order = self._direct_order(window, start)

        outcome = apply_low_fill_policy(window, start.date())

        self.assertEqual(outcome, 'ROLLED')
        order.refresh_from_db()
        self.assertEqual(order.scheduled_for, start + timedelta(days=7))

    def test_roll_moves_the_preorder_batch_to_the_new_slot(self):
        start = _booking_start()
        window = self._window(start=start, min_orders=3)
        self._set_policy('ROLL')
        order = self._place(window, scheduled_for=start)
        batch = BatchPipeline.on_order_confirmed(order)
        self.assertEqual(batch.preorder_slot_start, slot_window_for(start)[0])

        outcome = apply_low_fill_policy(window, start.date())

        self.assertEqual(outcome, 'ROLLED')
        batch.refresh_from_db()
        target = start + timedelta(days=7)
        self.assertEqual(
            batch.preorder_slot_start, slot_window_for(target)[0],
        )

    def test_cancel_policy_refunds_and_cancels(self):
        start = _booking_start()
        window = self._window(start=start, min_orders=3)
        self._set_policy('CANCEL')
        order = self._direct_order(window, start)

        outcome = apply_low_fill_policy(window, start.date())

        self.assertEqual(outcome, 'CANCELLED')
        order.refresh_from_db()
        self.assertEqual(order.status, 'CANCELLED')

    def test_full_window_is_left_alone(self):
        start = _booking_start()
        window = self._window(start=start, min_orders=1)
        self._set_policy('CANCEL')
        self._direct_order(window, start)

        self.assertEqual(apply_low_fill_policy(window, start.date()), 'OK')


class DropOccupancyTests(DropTestBase):
    """The calendar says which kitchen already holds an occurrence."""

    def test_window_reports_the_kitchen_that_claimed_it(self):
        start = _booking_start()
        window = self._window(start=start)
        self._direct_order(window, start)

        occurrence = describe_window(window, start)

        self.assertEqual(
            occurrence['serving_restaurant_id'], str(self.restaurant.id),
        )
        self.assertEqual(occurrence['serving_restaurant_name'], 'Drop Diner')
        self.assertIsNone(occurrence['open_pool_id'])
        self.assertGreater(occurrence['service_fee'], 0)

    def test_unclaimed_window_has_no_serving_kitchen(self):
        start = _booking_start()
        window = self._window(start=start)

        occurrence = describe_window(window, start)

        self.assertIsNone(occurrence['serving_restaurant_id'])
        self.assertIsNone(occurrence['serving_restaurant_name'])
        self.assertIsNone(occurrence['open_pool_id'])


class DropDefaultsTests(TestCase):
    def test_economics_defaults_match_one_profitable_trip(self):
        from apps.pricing.models import PricingPolicy

        self.assertEqual(DropPoint._meta.get_field('min_orders').default, 4)
        self.assertEqual(DropPoint._meta.get_field('max_orders').default, 5)
        self.assertEqual(
            PricingPolicy._meta.get_field('drop_low_fill_policy').default,
            PricingPolicy.DROP_LOW_FILL_ROLL,
        )


class DropCalendarVisibilityTests(DropTestBase):
    """The home feed advertises drops before the customer has an account."""

    def test_anonymous_visitors_can_read_the_calendar(self):
        from rest_framework.test import APIClient

        self._window(start=_booking_start())

        response = APIClient().get(
            '/api/drop-calendar/',
            {'lat': -6.79, 'lng': 39.21, 'days': 3},
        )

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.data['count'], 1)
