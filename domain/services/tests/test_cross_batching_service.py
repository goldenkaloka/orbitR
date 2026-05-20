from django.test import TestCase
from django.contrib.gis.geos import Point
from decimal import Decimal
from unittest.mock import patch, MagicMock

from apps.restaurants.models import Restaurant
from apps.pricing.models import PricingPolicy
from apps.orders.models import Order, DeliveryBatch, BatchingStrategy
from apps.users.models import User
from apps.geo.models import OperationalZone, ZoneH3Index
from domain.services.cross_batching_service import CrossBatchingService, CROSS_BATCH_DISCOUNT
from domain.entities.route import Route


class CrossBatchingServiceTests(TestCase):
    """Unit tests for CrossBatchingService"""

    def setUp(self):
        self.user = User.objects.create_user(username='cross_batch_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Cross Batching Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )
        self.restaurant2 = Restaurant.objects.create(
            name="Cross Batching Restaurant 2",
            owner=self.user,
            geo_location=Point(39.2050, -6.7950),
            max_delivery_radius_km=10.0
        )

        self.policy = PricingPolicy.objects.create(
            name="Cross Batch Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'),
            driver_per_stop_rate=Decimal('30.00')
        )

    @patch('domain.services.cross_batching_service.MapboxRoutingService.get_route_for_batch')
    def test_route_optimization_uses_mapbox(self, mock_route):
        """Test that cross-batching uses Mapbox for route optimization"""
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
            restaurant=self.restaurant2,
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
                {'order_id': str(order2.id), 'type': 'pickup', 'estimated_arrival_min': 10},
                {'order_id': str(order1.id), 'type': 'delivery', 'estimated_arrival_min': 15},
                {'order_id': str(order2.id), 'type': 'delivery', 'estimated_arrival_min': 25}
            ]
        }
        mock_route.return_value = Route.from_dict(mock_route_dict)

        result = CrossBatchingService._optimize_route([order1, order2])

        self.assertIsNotNone(result)
        mock_route.assert_called()


class CrossBatchingServiceDriverPayoutTests(TestCase):
    """Tests for driver payout calculation in cross-batching"""

    def setUp(self):
        self.user = User.objects.create_user(username='cross_payout_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Payout Test Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )

        self.policy = PricingPolicy.objects.create(
            name="Payout Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'),
            driver_per_stop_rate=Decimal('30.00')
        )

    def test_cross_batch_discount_constant(self):
        """Test that CROSS_BATCH_DISCOUNT is 15%"""
        self.assertEqual(CROSS_BATCH_DISCOUNT, Decimal('0.15'))


class CrossBatchingServiceClusteringTests(TestCase):
    """Tests for order clustering in cross-batching"""

    def test_cluster_by_delivery_h3_returns_clusters(self):
        """Test that _cluster_orders_by_delivery_h3 returns clusters"""
        user = User.objects.create_user(username='cluster_user', password='testpass')
        restaurant = Restaurant.objects.create(
            name="Cluster Restaurant",
            owner=user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )

        order1 = Order(
            restaurant=restaurant,
            customer=user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Address 1",
            status='READY',
            h3_index='892834b0c9fffff'
        )
        order2 = Order(
            restaurant=restaurant,
            customer=user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Address 2",
            status='READY',
            h3_index='892834b0c9fffff'
        )
        order3 = Order(
            restaurant=restaurant,
            customer=user,
            delivery_location=Point(39.2200, -6.8000),
            delivery_address="Address 3",
            status='READY',
            h3_index='892834b0c8fffff'
        )

        orders = [order1, order2, order3]
        clusters = CrossBatchingService._cluster_orders_by_delivery_h3(orders, resolution=10)

        self.assertIsInstance(clusters, dict)

    def test_merge_nearby_clusters_combines_close_clusters(self):
        """Test that _merge_nearby_clusters combines nearby H3 cells"""
        clusters = {
            'abc123': [],
            'abc124': [],
            'xyz789': []
        }

        merged = CrossBatchingService._merge_nearby_clusters(clusters, resolution=10)

        self.assertIsInstance(merged, dict)


class CrossBatchingServiceDriverMatcherTests(TestCase):
    """Tests for driver matching in cross-batching"""

    def setUp(self):
        self.user = User.objects.create_user(username='matcher_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Matcher Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )

    def test_find_best_driver_returns_none_when_no_route(self):
        """Test that find_best_driver returns None when no route optimization"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            route_optimization=None,
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00')
        )

        driver = CrossBatchingService.find_best_driver_for_batch(batch)
        self.assertIsNone(driver)


class CrossBatchingServiceAlgorithmTests(TestCase):
    """Tests for routing algorithm in cross-batching"""

    def test_nearest_neighbor_route_returns_list(self):
        """Test that _nearest_neighbor_route returns a list"""
        pickups = [
            {'lat': 39.2026, 'lng': -6.7924, 'restaurant_id': 1, 'name': 'R1'},
            {'lat': 39.2050, 'lng': -6.7950, 'restaurant_id': 2, 'name': 'R2'}
        ]
        deliveries = [
            {'lat': 39.2100, 'lng': -6.7924, 'order_id': '1', 'address': 'Addr1'},
            {'lat': 39.2120, 'lng': -6.7930, 'order_id': '2', 'address': 'Addr2'}
        ]

        route = CrossBatchingService._nearest_neighbor_route(pickups, deliveries)

        self.assertIsInstance(route, list)

    def test_calculate_distance_returns_float(self):
        """Test that _calculate_distance returns a float"""
        route = [
            {'type': 'pickup', 'lat': 39.2026, 'lng': -6.7924, 'restaurant_id': 1},
            {'type': 'delivery', 'lat': 39.2100, 'lng': -6.7924, 'order_id': '1'}
        ]
        restaurant_locs = {1: {'lat': 39.2026, 'lng': -6.7924, 'name': 'R1'}}
        order_dict = {'1': type('Order', (), {'restaurant_id': 1, 'delivery_location': Point(39.2100, -6.7924)})()}

        distance = CrossBatchingService._calculate_distance(route, restaurant_locs, order_dict)

        self.assertIsInstance(distance, (int, float))
        self.assertGreater(distance, 0)

    def test_estimate_time_returns_minutes(self):
        """Test that _estimate_time returns estimated minutes"""
        distance = 10.0
        num_pickups = 2
        num_deliveries = 2

        time_estimate = CrossBatchingService._estimate_time(distance, num_pickups, num_deliveries)

        self.assertIsInstance(time_estimate, (int, float))
        self.assertGreater(time_estimate, 0)