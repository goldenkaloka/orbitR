from decimal import Decimal

from apps.delivery.models import Driver
from apps.orders.models import DeliveryBatch, Order
from apps.pricing.models import PricingPolicy
from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import TestCase
from domain.services.payout_service import PayoutService


class _StubOrder:
    """Unsaved stand-in so the quote maths can be tested without a trip."""

    def __init__(self, delivery_fee, service_fee=Decimal('0'), drop_window_id=None):
        self.delivery_fee = delivery_fee
        self.service_fee = service_fee
        self.drop_window_id = drop_window_id


class PayoutQuoteTests(TestCase):
    def setUp(self):
        cache.clear()
        PricingPolicy.objects.create(
            name='Payout Test', is_active=True,
            driver_min_guarantee=Decimal('1500.00'),
            driver_per_km_rate=Decimal('500.00'),
            driver_per_stop_rate=Decimal('300.00'),
            single_order_driver_share=Decimal('0.95'),
            drop_trip_payout=Decimal('3000.00'),
        )

    def test_drop_trip_pays_the_flat_fee_uncapped(self):
        orders = [
            _StubOrder(Decimal('500.00'), Decimal('300.00'), drop_window_id='w1'),
            _StubOrder(Decimal('500.00'), Decimal('300.00'), drop_window_id='w1'),
        ]

        payout, collected = PayoutService.quote(orders)

        self.assertEqual(payout, Decimal('3000.00'))
        self.assertEqual(collected, Decimal('1600.00'))

    def test_routed_trip_pays_guarantee_plus_distance_and_stops(self):
        orders = [
            _StubOrder(Decimal('3000.00'), Decimal('300.00')),
            _StubOrder(Decimal('3000.00'), Decimal('300.00')),
            _StubOrder(Decimal('3000.00'), Decimal('300.00')),
        ]
        route = {
            'total_distance_km': 12.4,
            'stop_sequence': [{'type': 'pickup'}, {'type': 'delivery'}] * 2,
        }

        payout, collected = PayoutService.quote(orders, route_dict=route)

        # 1500 + 12.4 * 500 + 4 stops * 300
        self.assertEqual(payout, Decimal('8900.00'))
        self.assertEqual(collected, Decimal('9900.00'))

    def test_payout_is_capped_at_what_was_collected(self):
        orders = [_StubOrder(Decimal('2000.00'), Decimal('0.00'))] * 2
        route = {'total_distance_km': 12.4, 'stop_sequence': [{}] * 4}

        payout, collected = PayoutService.quote(orders, route_dict=route)

        self.assertEqual(collected, Decimal('4000.00'))
        self.assertEqual(payout, Decimal('4000.00'))

    def test_explicit_stops_win_over_the_route(self):
        orders = [_StubOrder(Decimal('3000.00'), Decimal('300.00'))]
        route = {'total_distance_km': 4.0, 'stop_sequence': [{}] * 9}

        payout, _ = PayoutService.quote(orders, route_dict=route, stops=2)

        # 1500 + 4 km * 500 + 2 stops * 300, not nine stops.
        self.assertEqual(payout, Decimal('4100.00'))

    def test_no_route_pays_the_single_order_share_of_delivery_fees(self):
        orders = [_StubOrder(Decimal('3000.00'), Decimal('300.00'))]

        payout, collected = PayoutService.quote(orders)

        self.assertEqual(payout, Decimal('2850.00'))
        self.assertEqual(collected, Decimal('3300.00'))

    def test_minimum_guarantee_survives_a_tiny_fee(self):
        orders = [_StubOrder(Decimal('500.00'), Decimal('300.00'))]

        payout, collected = PayoutService.quote(orders)

        self.assertEqual(collected, Decimal('800.00'))
        self.assertEqual(payout, Decimal('800.00'))


class PayoutOrderShareTests(TestCase):
    def setUp(self):
        cache.clear()
        PricingPolicy.objects.create(
            name='Payout Share Test', is_active=True,
            single_order_driver_share=Decimal('0.95'),
        )
        owner = User.objects.create_user(username='share_owner', password='pass', role='vendor')
        self.restaurant = Restaurant.objects.create(
            owner=owner, name='Share Kitchen',
            geo_location=Point(39.2083, -6.7924), max_delivery_radius_km=10.0,
        )
        self.customer = User.objects.create_user(username='share_cust', password='pass')
        rider = User.objects.create_user(username='share_rider', password='pass', role='driver')
        self.driver = Driver.objects.create(
            user=rider, status='AVAILABLE',
            location=Point(39.2083, -6.7924), driver_type='platform',
        )

    def _order(self, **kwargs):
        defaults = dict(
            customer=self.customer, restaurant=self.restaurant,
            delivery_address='Addr', items_subtotal=Decimal('10000.00'),
            delivery_fee=Decimal('3000.00'), service_fee=Decimal('300.00'),
            total_amount=Decimal('13300.00'), status='CONFIRMED',
        )
        defaults.update(kwargs)
        return Order.objects.create(**defaults)

    def test_batch_payout_is_split_evenly(self):
        batch = DeliveryBatch.objects.create(
            restaurant=self.restaurant, driver=self.driver,
            status='DISPATCHED', driver_payout=Decimal('6000.00'),
        )
        order = self._order(batch=batch, driver=self.driver)
        self._order(batch=batch, driver=self.driver)
        self._order(batch=batch, driver=self.driver)

        self.assertEqual(PayoutService.order_share(order), Decimal('2000.00'))

    def test_unbatched_order_pays_the_single_order_share(self):
        order = self._order(driver=self.driver)

        self.assertEqual(PayoutService.order_share(order), Decimal('2850.00'))

    def test_order_without_a_rider_pays_nothing(self):
        order = self._order()

        self.assertEqual(PayoutService.order_share(order), Decimal('0'))
