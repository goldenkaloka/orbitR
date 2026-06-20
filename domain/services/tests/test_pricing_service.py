from decimal import Decimal
from unittest.mock import MagicMock, patch

from apps.geo.models import OperationalZone, ZoneH3Index
from apps.orders.models import DeliveryBatch, Order
from apps.pricing.models import DistanceTier, PricingPolicy, ZonePricingTier
from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from domain.services.pricing_service import PricingService


class PricingServiceTests(TestCase):
    """Unit tests for PricingService"""

    def setUp(self):
        self.user = User.objects.create_user(username='test_pricing_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Test Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )

        self.policy = PricingPolicy.objects.create(
            name="Test Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            driver_min_guarantee=Decimal('2000.00'),
            driver_per_km_rate=Decimal('500.00'),
            driver_per_stop_rate=Decimal('300.00'),
            single_order_driver_share=Decimal('0.90')
        )

    def test_calculate_delivery_fee_returns_correct_fee(self):
        """Test that calculate_delivery_fee returns correct fee based on distance"""
        delivery_loc = Point(39.2226, -6.7924)

        fee, service_fee, log = PricingService.calculate_delivery_fee(
            self.restaurant, delivery_loc
        )

        self.assertIsNotNone(fee)
        self.assertGreater(fee, Decimal('1000.00'))
        self.assertEqual(service_fee, Decimal('300.00'))

    def test_calculate_delivery_fee_missing_location_returns_default(self):
        """Test that missing location returns default fee"""
        fee, service_fee, log = PricingService.calculate_delivery_fee(
            self.restaurant, None
        )

        self.assertEqual(fee, Decimal('1000.00'))
        self.assertEqual(service_fee, Decimal('300.00'))

    def test_calculate_delivery_fee_missing_restaurant_geo_returns_default(self):
        """Test that missing restaurant geo location returns default fee"""
        restaurant_no_geo = Restaurant.objects.create(
            name="Restaurant No Geo",
            owner=self.user,
            geo_location=Point(0, 0),
            max_delivery_radius_km=10.0
        )
        delivery_loc = Point(39.2226, -6.7924)

        fee, service_fee, log = PricingService.calculate_delivery_fee(
            restaurant_no_geo, delivery_loc
        )

        self.assertEqual(fee, Decimal('1000.00'))

    def test_calculate_batch_fee_single_order(self):
        """Test batch fee calculation for single order"""
        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='READY',
            delivery_fee=Decimal('1500.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('5000.00'),
            total_amount=Decimal('6800.00')
        )

        result = PricingService.calculate_batch_fee([order], 'SINGLE_ORDER')

        self.assertEqual(result['original_total'], Decimal('1500.00'))
        self.assertEqual(result['discounted_total'], Decimal('1500.00'))
        self.assertEqual(result['discount_rate'], Decimal('0'))

    def test_apply_batch_fees_updates_orders(self):
        """Test that apply_batch_fees correctly updates order fees"""
        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='READY',
            delivery_fee=Decimal('2000.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('5000.00'),
            total_amount=Decimal('7300.00')
        )

        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00'),
            company_profit=Decimal('300.00')
        )
        order.batch = batch
        order.save()

        PricingService.apply_batch_fees(batch)

        order.refresh_from_db()
        self.assertEqual(order.original_delivery_fee, Decimal('2000.00'))
        self.assertEqual(order.delivery_fee, Decimal('2000.00'))

    def test_get_active_policy_returns_cached(self):
        """Test that get_active_policy returns cached policy"""
        policy = PricingService.get_active_policy()

        self.assertIsNotNone(policy)
        self.assertEqual(policy.name, "Test Policy")

    def test_get_distance_tier_multiplier_finds_correct_tier(self):
        """Test distance tier multiplier selection"""
        DistanceTier.objects.create(
            policy=self.policy,
            min_km=0,
            max_km=2,
            multiplier=Decimal('1.00')
        )
        DistanceTier.objects.create(
            policy=self.policy,
            min_km=2,
            max_km=5,
            multiplier=Decimal('1.50')
        )

        tiers = DistanceTier.objects.filter(policy=self.policy).order_by('min_km')

        self.assertEqual(PricingService.get_distance_tier_multiplier(tiers, 1), Decimal('1.00'))
        self.assertEqual(PricingService.get_distance_tier_multiplier(tiers, 3), Decimal('1.50'))
        self.assertEqual(PricingService.get_distance_tier_multiplier(tiers, 10), Decimal('1.50'))


class PricingServiceEdgeCases(TestCase):
    """Edge case tests for PricingService"""

    def setUp(self):
        self.user = User.objects.create_user(username='edge_test_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Edge Test Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )
        self.policy = PricingPolicy.objects.create(
            name="Edge Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            driver_min_guarantee=Decimal('2000.00'),
            driver_per_km_rate=Decimal('500.00'),
            driver_per_stop_rate=Decimal('300.00'),
        )

    def test_very_short_distance(self):
        """Test fee calculation for very short distance (<1km)"""
        very_close = Point(39.2030, -6.7924)

        fee, _, _ = PricingService.calculate_delivery_fee(
            self.restaurant, very_close
        )

        self.assertGreaterEqual(fee, Decimal('1000.00'))

    def test_very_long_distance(self):
        """Test fee calculation for very long distance (>20km)"""
        far = Point(39.5026, -6.7924)

        fee, _, _ = PricingService.calculate_delivery_fee(
            self.restaurant, far
        )

        self.assertGreater(fee, Decimal('10000.00'))