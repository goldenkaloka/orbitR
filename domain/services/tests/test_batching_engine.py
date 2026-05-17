from django.test import TestCase
from django.contrib.gis.geos import Point
from decimal import Decimal
from unittest.mock import patch, MagicMock

from apps.restaurants.models import Restaurant
from apps.geo.models import OperationalZone, ZoneH3Index
from apps.pricing.models import PricingPolicy
from apps.orders.models import Order, DeliveryBatch, BatchingStrategy
from apps.users.models import User
from domain.services.batching_engine import BatchingEngine
from domain.services.mapbox_routing_service import MapboxRoutingService
from domain.entities.route import Route


class BatchingEngineSingleOrderTests(TestCase):
    """Tests for single order batch creation"""

    def setUp(self):
        self.user = User.objects.create_user(username='batch_test_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Batching Test Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )

        self.policy = PricingPolicy.objects.create(
            name="Batching Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'),
            driver_per_stop_rate=Decimal('30.00'),
            single_order_driver_share=Decimal('0.90')
        )

    @patch('domain.services.batching_engine.MapboxRoutingService.get_route_for_batch')
    def test_create_single_order_batch_percentage_payout(self, mock_route):
        """Test that single order uses percentage-based payout"""
        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='READY',
            delivery_fee=Decimal('2000.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('5000.00'),
            total_amount=Decimal('7300.00'),
            h3_index='892834b0c9fffff'
        )

        mock_route_dict = {
            'total_distance_km': 5.0,
            'total_duration_min': 20,
            'stop_sequence': [
                {'order_id': str(order.id), 'type': 'pickup', 'estimated_arrival_min': 0},
                {'order_id': str(order.id), 'type': 'delivery', 'estimated_arrival_min': 20}
            ]
        }
        mock_route.return_value = Route.from_dict(mock_route_dict)

        batch = BatchingEngine._create_single_order_batch(order)

        self.assertIsNotNone(batch)
        self.assertEqual(batch.batch_type, 'SINGLE_ORDER')
        self.assertEqual(batch.driver_payout, Decimal('1800.00'))

    @patch('domain.services.batching_engine.MapboxRoutingService.get_route_for_batch')
    def test_create_single_order_batch_payout_cap(self, mock_route):
        """Test that single order payout is capped at user_paid_total"""
        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='READY',
            delivery_fee=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('2000.00'),
            total_amount=Decimal('2800.00'),
            h3_index='892834b0c9fffff'
        )

        mock_route_dict = {
            'total_distance_km': 1.0,
            'total_duration_min': 10,
            'stop_sequence': [
                {'order_id': str(order.id), 'type': 'pickup', 'estimated_arrival_min': 0},
                {'order_id': str(order.id), 'type': 'delivery', 'estimated_arrival_min': 10}
            ]
        }
        mock_route.return_value = Route.from_dict(mock_route_dict)

        batch = BatchingEngine._create_single_order_batch(order)

        self.assertIsNotNone(batch)
        self.assertEqual(batch.driver_payout, Decimal('800.00'))
        self.assertEqual(batch.company_profit, Decimal('0'))

    @patch('domain.services.batching_engine.MapboxRoutingService.get_route_for_batch')
    def test_create_single_order_batch_route_optimization_stored(self, mock_route):
        """Test that route optimization is stored in batch"""
        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='READY',
            delivery_fee=Decimal('2000.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('5000.00'),
            total_amount=Decimal('7300.00'),
            h3_index='892834b0c9fffff'
        )

        mock_route_dict = {
            'total_distance_km': 5.0,
            'total_duration_min': 20,
            'stop_sequence': [
                {'order_id': str(order.id), 'type': 'pickup', 'estimated_arrival_min': 0},
                {'order_id': str(order.id), 'type': 'delivery', 'estimated_arrival_min': 20}
            ],
            'geometry': {'type': 'LineString', 'coordinates': [[39.2026, -6.7924], [39.2100, -6.7924]]}
        }
        mock_route.return_value = Route.from_dict(mock_route_dict)

        batch = BatchingEngine._create_single_order_batch(order)

        self.assertIsNotNone(batch)
        self.assertIsNotNone(batch.route_optimization)
        self.assertEqual(batch.route_distance_km, Decimal('5.0'))


class BatchingEngineMultiOrderTests(TestCase):
    """Tests for multi-order batch creation"""

    def setUp(self):
        self.user = User.objects.create_user(username='multi_batch_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Multi Batch Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )

        self.policy = PricingPolicy.objects.create(
            name="Multi Batch Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'),
            driver_per_stop_rate=Decimal('30.00')
        )

        BatchingStrategy.objects.update_or_create(
            name='SINGLE_RESTAURANT',
            defaults={
                'is_active': True,
                'cluster_by': 'restaurant',
                'h3_resolution': 9,
                'neighbor_ring_k': 1,
                'constraints': {
                    'max_orders': 5,
                    'max_restaurants': 1,
                    'max_distance_km': 10,
                    'min_orders': 2
                },
                'route_optimization': {'enabled': True}
            }
        )

    @patch('domain.services.batching_engine.MapboxRoutingService.get_route_for_batch')
    def test_multi_order_batch_driver_payout_calculation(self, mock_route):
        """Test driver payout calculation for multi-order batch"""
        order1 = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Address 1",
            status='READY',
            delivery_fee=Decimal('1500.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('3000.00'),
            total_amount=Decimal('4800.00'),
            h3_index='892834b0c9fffff'
        )
        order2 = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2120, -6.7930),
            delivery_address="Address 2",
            status='READY',
            delivery_fee=Decimal('1500.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('3000.00'),
            total_amount=Decimal('4800.00'),
            h3_index='892834b0c9fffff'
        )

        mock_route_dict = {
            'total_distance_km': 8.0,
            'total_duration_min': 25,
            'stop_sequence': [
                {'order_id': str(order1.id), 'type': 'pickup', 'estimated_arrival_min': 0},
                {'order_id': str(order1.id), 'type': 'delivery', 'estimated_arrival_min': 10},
                {'order_id': str(order2.id), 'type': 'pickup', 'estimated_arrival_min': 15},
                {'order_id': str(order2.id), 'type': 'delivery', 'estimated_arrival_min': 25}
            ]
        }
        mock_route.return_value = Route.from_dict(mock_route_dict)

        strategy = BatchingStrategy.objects.get(name='SINGLE_RESTAURANT')

        batch = BatchingEngine._create_batch([order1, order2], strategy, [], False)

        self.assertIsNotNone(batch)
        self.assertEqual(batch.batch_type, 'SINGLE_RESTAURANT')

    @patch('domain.services.batching_engine.MapboxRoutingService.get_route_for_batch')
    def test_multi_order_batch_payout_cap(self, mock_route):
        """Test that multi-order batch payout is capped to never go negative"""
        order1 = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Address 1",
            status='READY',
            delivery_fee=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('2000.00'),
            total_amount=Decimal('2800.00'),
            h3_index='892834b0c9fffff'
        )
        order2 = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2105, -6.7928),
            delivery_address="Address 2",
            status='READY',
            delivery_fee=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('2000.00'),
            total_amount=Decimal('2800.00'),
            h3_index='892834b0c9fffff'
        )

        mock_route_dict = {
            'total_distance_km': 50.0,
            'total_duration_min': 60,
            'stop_sequence': [
                {'order_id': str(order1.id), 'type': 'pickup', 'estimated_arrival_min': 0},
                {'order_id': str(order1.id), 'type': 'delivery', 'estimated_arrival_min': 30},
                {'order_id': str(order2.id), 'type': 'pickup', 'estimated_arrival_min': 35},
                {'order_id': str(order2.id), 'type': 'delivery', 'estimated_arrival_min': 60}
            ]
        }
        mock_route.return_value = Route.from_dict(mock_route_dict)

        strategy = BatchingStrategy.objects.get(name='SINGLE_RESTAURANT')

        batch = BatchingEngine._create_batch([order1, order2], strategy, [], False)

        self.assertIsNotNone(batch)
        self.assertGreaterEqual(batch.driver_payout, Decimal('0'))
        self.assertGreaterEqual(batch.company_profit, Decimal('0'))


class BatchingEngineStrategyTests(TestCase):
    """Tests for batching strategy handling"""

    def test_seed_strategies_creates_defaults(self):
        """Test that seed_strategies creates default strategies"""
        BatchingStrategy.objects.all().delete()

        created = BatchingEngine.seed_strategies()

        self.assertTrue(len(created) > 0)

        strategy_names = [s[0] for s in created]
        self.assertIn('SINGLE_RESTAURANT', strategy_names)

    def test_get_constraint_returns_default(self):
        """Test strategy constraint retrieval with defaults"""
        strategy, _ = BatchingStrategy.objects.update_or_create(
            name='TEST_STRATEGY',
            defaults={
                'is_active': True,
                'cluster_by': 'restaurant',
                'constraints': {'min_orders': 2}
            }
        )

        self.assertEqual(strategy.get_constraint('min_orders', 1), 2)
        self.assertEqual(strategy.get_constraint('max_orders', 10), 10)

    def test_get_constraint_returns_none_for_missing(self):
        """Test strategy constraint returns None for missing key"""
        strategy, _ = BatchingStrategy.objects.update_or_create(
            name='EMPTY_STRATEGY',
            defaults={
                'is_active': True,
                'cluster_by': 'restaurant',
                'constraints': {}
            }
        )

        self.assertIsNone(strategy.get_constraint('max_orders'))


class BatchingEngineCompanyProfitTests(TestCase):
    """Tests for company profit calculation"""

    def setUp(self):
        self.user = User.objects.create_user(username='profit_test_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Profit Test Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )

        self.policy = PricingPolicy.objects.create(
            name="Profit Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'),
            driver_per_stop_rate=Decimal('30.00')
        )

    @patch('domain.services.batching_engine.MapboxRoutingService.get_route_for_batch')
    def test_company_profit_calculated_correctly(self, mock_route):
        """Test company profit = user_paid - driver_payout"""
        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='READY',
            delivery_fee=Decimal('2000.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('5000.00'),
            total_amount=Decimal('7300.00'),
            h3_index='892834b0c9fffff'
        )

        mock_route_dict = {
            'total_distance_km': 5.0,
            'total_duration_min': 20,
            'stop_sequence': [
                {'order_id': str(order.id), 'type': 'pickup', 'estimated_arrival_min': 0},
                {'order_id': str(order.id), 'type': 'delivery', 'estimated_arrival_min': 20}
            ]
        }
        mock_route.return_value = Route.from_dict(mock_route_dict)

        batch = BatchingEngine._create_single_order_batch(order)

        user_paid = Decimal('2300.00')
        expected_profit = user_paid - batch.driver_payout

        self.assertEqual(batch.company_profit, expected_profit)
        self.assertGreaterEqual(batch.company_profit, Decimal('0'))

    @patch('domain.services.batching_engine.MapboxRoutingService.get_route_for_batch')
    def test_company_profit_never_negative(self, mock_route):
        """Test that company profit is never negative after cap"""
        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='READY',
            delivery_fee=Decimal('2000.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('5000.00'),
            total_amount=Decimal('7300.00'),
            h3_index='892834b0c9fffff'
        )

        mock_route_dict = {
            'total_distance_km': 5.0,
            'total_duration_min': 20,
            'stop_sequence': [
                {'order_id': str(order.id), 'type': 'pickup', 'estimated_arrival_min': 0},
                {'order_id': str(order.id), 'type': 'delivery', 'estimated_arrival_min': 20}
            ]
        }
        mock_route.return_value = Route.from_dict(mock_route_dict)

        batch = BatchingEngine._create_single_order_batch(order)

        self.assertGreaterEqual(batch.company_profit, Decimal('0'))