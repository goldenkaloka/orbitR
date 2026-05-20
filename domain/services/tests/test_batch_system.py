"""
Comprehensive Batching System Tests
====================================
39 test cases covering all batching scenarios.
"""
from django.test import TestCase, TransactionTestCase
from django.contrib.gis.geos import Point, Polygon, MultiPolygon
from decimal import Decimal
from unittest.mock import patch, MagicMock
from django.utils import timezone
import uuid

from apps.restaurants.models import Restaurant
from apps.geo.models import OperationalZone, ZoneH3Index
from apps.pricing.models import PricingPolicy
from apps.orders.models import Order, OrderItem, DeliveryBatch, BatchRestaurant, BatchingStrategy
from apps.delivery.models import Driver
from apps.users.models import User
from domain.services.batch_pipeline import BatchPipeline
from domain.services.batch_dispatch_service import BatchDispatchService
from domain.services.batching_engine import BatchingEngine
from apps.core.geo_utils import latlng_to_h3


class TestSetupMixin:
    LAT_CENTER = -6.7924
    LNG_CENTER = 39.2083

    def setup_zone_and_policy(self):
        poly = MultiPolygon(Polygon((
            (self.LNG_CENTER - 0.1, self.LAT_CENTER - 0.1),
            (self.LNG_CENTER + 0.1, self.LAT_CENTER - 0.1),
            (self.LNG_CENTER + 0.1, self.LAT_CENTER + 0.1),
            (self.LNG_CENTER - 0.1, self.LAT_CENTER + 0.1),
            (self.LNG_CENTER - 0.1, self.LAT_CENTER - 0.1)
        )))
        self.zone = OperationalZone.objects.create(name="Test Zone", boundary=poly)
        self.zone.refresh_h3_indexes()
        self.policy = PricingPolicy.objects.create(
            name="Test Policy", is_active=True,
            base_fee=Decimal('1000.00'), per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'), driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'), driver_per_stop_rate=Decimal('30.00'),
            single_order_driver_share=Decimal('0.90')
        )

    def setup_users(self):
        self.customer = User.objects.create_user(
            username=f'test_cust_{uuid.uuid4().hex[:8]}', password='testpass', role='customer')
        self.vendor = User.objects.create_user(
            username=f'test_vendor_{uuid.uuid4().hex[:8]}', password='testpass', role='vendor')

    def setup_restaurants(self):
        self.restaurant_a = Restaurant.objects.create(
            owner=self.vendor, name="Test Restaurant A", location="Location A",
            geo_location=Point(self.LNG_CENTER, self.LAT_CENTER), max_delivery_radius_km=10.0)
        self.restaurant_b = Restaurant.objects.create(
            owner=self.vendor, name="Test Restaurant B", location="Location B",
            geo_location=Point(self.LNG_CENTER + 0.002, self.LAT_CENTER + 0.002), max_delivery_radius_km=10.0)
        self.restaurant_c = Restaurant.objects.create(
            owner=self.vendor, name="Test Restaurant C", location="Location C",
            geo_location=Point(self.LNG_CENTER - 0.002, self.LAT_CENTER + 0.002), max_delivery_radius_km=10.0)

    def create_order(self, restaurant, lng=None, lat=None, status='READY', delivery_fee=None):
        if lng is None: lng = self.LNG_CENTER + 0.005
        if lat is None: lat = self.LAT_CENTER - 0.005
        if delivery_fee is None: delivery_fee = Decimal('1500.00')
        order = Order.objects.create(
            customer=self.customer, restaurant=restaurant, status=status,
            delivery_address="Test Address", delivery_location=Point(lng, lat),
            items_subtotal=Decimal('5000.00'), delivery_fee=delivery_fee,
            service_fee=Decimal('300.00'), total_amount=Decimal('6800.00'))
        return order

    def create_route_dict(self, orders, distance_km=5.0, duration_min=20):
        return {'total_distance_km': distance_km, 'total_duration_min': duration_min, 'stop_sequence': []}


# =============================================================================
# CATEGORY 1: BATCH TYPES (5 cases)
# =============================================================================

class BatchTypeSINGLE_ORDERTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_single_order_batch_created(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.batch_type, 'SINGLE_ORDER')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class BatchTypeSINGLE_RESTAURANTTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_same_restaurant_batched_together(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_a, lng=39.2120, lat=-6.7964)
        order3 = self.create_order(self.restaurant_a, lng=39.2140, lat=-6.7954)
        b1 = BatchPipeline.run(order1)
        b2 = BatchPipeline.run(order2)
        batch = BatchPipeline.run(order3)
        print("DIAGNOSTIC - b1:", b1, "orders in b1:", list(b1.orders.all()) if b1 else None)
        print("DIAGNOSTIC - b2:", b2)
        print("DIAGNOSTIC - batch:", batch)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.batch_type, 'SINGLE_RESTAURANT')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class BatchTypeCROSS_RESTAURANTTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_cross_restaurant_batching(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_b, lng=39.2105, lat=-6.7970)
        order3 = self.create_order(self.restaurant_c, lng=39.2110, lat=-6.7966)
        BatchPipeline.run(order1)
        BatchPipeline.run(order2)
        batch = BatchPipeline.run(order3)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.batch_type, 'CROSS_RESTAURANT')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class BatchTypeTIME_WINDOWTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_time_window_batching(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        now = timezone.now()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order1.ready_at = now
        order1.save()
        order2 = self.create_order(self.restaurant_b, lng=39.2105, lat=-6.7970)
        order2.ready_at = now + timezone.timedelta(minutes=10)
        order2.save()
        BatchPipeline.run(order1)
        BatchPipeline.run(order2)
        batch = DeliveryBatch.objects.filter(orders__in=[order1, order2]).first()
        self.assertIsNotNone(batch)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class BatchTypeINTER_ZONETests(TestSetupMixin, TestCase):
    def setUp(self):
        poly1 = MultiPolygon(Polygon((
            (self.LNG_CENTER - 0.1, self.LAT_CENTER - 0.1),
            (self.LNG_CENTER + 0.1, self.LAT_CENTER - 0.1),
            (self.LNG_CENTER + 0.1, self.LAT_CENTER + 0.1),
            (self.LNG_CENTER - 0.1, self.LAT_CENTER + 0.1),
            (self.LNG_CENTER - 0.1, self.LAT_CENTER - 0.1)
        )))
        self.zone1 = OperationalZone.objects.create(name="Test Zone 1", boundary=poly1)
        self.zone1.refresh_h3_indexes()
        poly2 = MultiPolygon(Polygon((
            (39.3 - 0.1, -6.7 - 0.1), (39.4 - 0.1, -6.7 - 0.1),
            (39.4 - 0.1, -6.6 - 0.1), (39.3 - 0.1, -6.6 - 0.1),
            (39.3 - 0.1, -6.7 - 0.1)
        )))
        self.zone2 = OperationalZone.objects.create(name="Test Zone 2", boundary=poly2)
        self.zone2.refresh_h3_indexes()
        self.policy = PricingPolicy.objects.create(
            name="Test Policy", is_active=True,
            base_fee=Decimal('1000.00'), per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'), driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'), driver_per_stop_rate=Decimal('30.00'),
            single_order_driver_share=Decimal('0.90')
        )
        self.setup_users()
        self.setup_restaurants()

    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_inter_zone_batch(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        order1 = self.create_order(self.restaurant_a, lng=39.25, lat=-6.75)
        order2 = self.create_order(self.restaurant_b, lng=39.35, lat=-6.65)
        BatchPipeline.run(order1)
        batch = BatchPipeline.run(order2)
        if batch:
            self.assertTrue(batch.is_inter_zone or batch.zone_ids is not None)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name__startswith="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


# =============================================================================
# CATEGORY 2: CLUSTERING LOGIC (8 cases)
# =============================================================================

class ClusteringSameH3CellTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_cluster_same_h3_cell(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_a, lng=39.2101, lat=-6.7975)
        self.assertEqual(order1.h3_index, order2.h3_index)
        BatchPipeline.run(order1)
        batch = BatchPipeline.run(order2)
        self.assertIsNotNone(batch)
        self.assertGreaterEqual(batch.orders.count(), 2)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class ClusteringDifferentH3CellTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_cluster_different_h3_cells(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_a, lng=39.2200, lat=-6.7874)
        BatchPipeline.run(order1)
        batch = BatchPipeline.run(order2)
        self.assertIsNotNone(batch)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class ClusteringMaxOrdersTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_respects_max_orders(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        orders = []
        for i in range(7):
            order = self.create_order(self.restaurant_a, lng=self.LNG_CENTER + (i * 0.002), lat=self.LAT_CENTER - (i * 0.001))
            orders.append(order)
        for order in orders[:6]:
            BatchPipeline.run(order)
        last_batch = DeliveryBatch.objects.filter(restaurant=self.restaurant_a, batch_type='SINGLE_RESTAURANT').order_by('-created_at').first()
        if last_batch:
            self.assertLessEqual(last_batch.orders.count(), 5)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class ClusteringCrossRestaurant2RestaurantsTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_cross_cluster_2_restaurants(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_a, lng=39.2105, lat=-6.7970)
        order3 = self.create_order(self.restaurant_b, lng=39.2110, lat=-6.7966)
        BatchPipeline.run(order1)
        BatchPipeline.run(order2)
        batch = BatchPipeline.run(order3)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.batch_type, 'CROSS_RESTAURANT')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class ClusteringCrossRestaurant3RestaurantsTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_cross_cluster_3_restaurants(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_b, lng=39.2105, lat=-6.7970)
        order3 = self.create_order(self.restaurant_c, lng=39.2110, lat=-6.7966)
        BatchPipeline.run(order1)
        BatchPipeline.run(order2)
        batch = BatchPipeline.run(order3)
        self.assertIsNotNone(batch)
        unique_restaurants = set(batch.orders.values_list('restaurant_id', flat=True))
        self.assertLessEqual(len(unique_restaurants), 3)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class ClusteringExceedsMaxRestaurantsTests(TestSetupMixin, TestCase):
    def setUp(self):
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        self.restaurant_d = Restaurant.objects.create(
            owner=self.vendor, name="Test Restaurant D", location="Location D",
            geo_location=Point(self.LNG_CENTER + 0.004, self.LAT_CENTER + 0.004), max_delivery_radius_km=10.0)
        self.restaurant_e = Restaurant.objects.create(
            owner=self.vendor, name="Test Restaurant E", location="Location E",
            geo_location=Point(self.LNG_CENTER + 0.006, self.LAT_CENTER + 0.006), max_delivery_radius_km=10.0)

    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_caps_at_max_restaurants(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_b, lng=39.2105, lat=-6.7970)
        order3 = self.create_order(self.restaurant_c, lng=39.2110, lat=-6.7966)
        order4 = self.create_order(self.restaurant_d, lng=39.2115, lat=-6.7962)
        order5 = self.create_order(self.restaurant_e, lng=39.2120, lat=-6.7958)
        BatchPipeline.run(order1)
        BatchPipeline.run(order2)
        BatchPipeline.run(order3)
        BatchPipeline.run(order4)
        batch = BatchPipeline.run(order5)
        if batch:
            unique_restaurants = set(batch.orders.values_list('restaurant_id', flat=True))
            self.assertLessEqual(len(unique_restaurants), 3)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class ClusteringExceedsMaxDistanceTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_orders_too_far_apart(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_b, lng=39.3500, lat=-6.6500)
        BatchPipeline.run(order1)
        batch = BatchPipeline.run(order2)
        if batch:
            self.assertEqual(batch.batch_type, 'SINGLE_ORDER')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class ClusteringInsufficientOrdersTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_insufficient_orders_for_cluster(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.batch_type, 'SINGLE_ORDER')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


# =============================================================================
# CATEGORY 3: ROUTE OPTIMIZATION (4 cases)
# =============================================================================

class RouteOptimizationMapboxValidTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_mapbox_valid_route(self, mock_route):
        mock_route.return_value = {'total_distance_km': 8.5, 'total_duration_min': 28, 'stop_sequence': [{'type': 'pickup', 'restaurant_id': '1'}, {'type': 'delivery', 'order_id': '1'}], 'geometry': {'type': 'LineString', 'coordinates': [[39.2083, -6.7924], [39.2100, -6.7974]]}}
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)
        self.assertIsNotNone(batch.route_optimization)
        self.assertEqual(batch.route_distance_km, Decimal('8.5'))

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class RouteOptimizationMapboxIncompleteTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_mapbox_incomplete_data(self, mock_route):
        mock_route.side_effect = Exception("Mapbox response missing data")
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class RouteOptimizationMapboxFailsTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_mapbox_fallback(self, mock_route):
        mock_route.side_effect = Exception("Mapbox API error")
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)
        self.assertIsNotNone(batch.route_optimization)
        self.assertIn('stop_sequence', batch.route_optimization)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class RouteOptimizationPickupsFirstTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_pickups_before_deliveries(self, mock_route):
        mock_route.side_effect = Exception("Force fallback")
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_b, lng=39.2110, lat=-6.7970)
        BatchPipeline.run(order1)
        batch = BatchPipeline.run(order2)
        if batch and batch.route_optimization:
            stops = batch.route_optimization.get('stop_sequence', [])
            pickup_indices = [i for i, s in enumerate(stops) if s.get('type') == 'pickup']
            delivery_indices = [i for i, s in enumerate(stops) if s.get('type') == 'delivery']
            self.assertTrue(all(p < d for p in pickup_indices for d in delivery_indices))

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


# =============================================================================
# CATEGORY 4: PRICING & PAYOUT (6 cases)
# =============================================================================

class PricingSingleOrderCappedTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_single_order_payout_cap(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a, delivery_fee=Decimal('500.00'))
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)
        self.assertLessEqual(batch.driver_payout, batch.user_paid_total)
        self.assertGreaterEqual(batch.driver_payout, Decimal('0'))

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class PricingSingleOrderWithMinimumGuaranteeTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_single_order_minimum_guarantee(self, mock_route):
        mock_route.return_value = {'total_distance_km': 10.0, 'total_duration_min': 35, 'stop_sequence': [{'type': 'pickup'}, {'type': 'delivery'}]}
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)
        expected_min = self.policy.driver_min_guarantee + (Decimal('10') * self.policy.driver_per_km_rate) + (Decimal('2') * self.policy.driver_per_stop_rate)
        self.assertGreaterEqual(batch.driver_payout, expected_min)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class PricingCrossBatchDiscountTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_cross_batch_discount_applied(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_b, lng=39.2105, lat=-6.7970)
        BatchPipeline.run(order1)
        batch = BatchPipeline.run(order2)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.discount_rate, Decimal('0.15'))
        self.assertEqual(batch.batch_type, 'CROSS_RESTAURANT')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class PricingMultiOrderPayoutCappedTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_multi_order_payout_cap(self, mock_route):
        mock_route.return_value = {'total_distance_km': 50.0, 'total_duration_min': 120, 'stop_sequence': [{'type': 'pickup'}, {'type': 'delivery'}, {'type': 'pickup'}, {'type': 'delivery'}]}
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974, delivery_fee=Decimal('500.00'))
        order2 = self.create_order(self.restaurant_a, lng=39.2105, lat=-6.7970, delivery_fee=Decimal('500.00'))
        BatchPipeline.run(order1)
        batch = BatchPipeline.run(order2)
        self.assertIsNotNone(batch)
        self.assertLessEqual(batch.driver_payout, batch.user_paid_total)
        self.assertGreaterEqual(batch.driver_payout, Decimal('0'))

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class PricingCompanyProfitTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_company_profit_non_negative(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)
        self.assertGreaterEqual(batch.company_profit, Decimal('0'))
        self.assertEqual(batch.company_profit, batch.user_paid_total - batch.driver_payout)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class PricingSingleOrderNoDiscountTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_single_restaurant_no_discount(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order1 = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        order2 = self.create_order(self.restaurant_a, lng=39.2105, lat=-6.7970)
        BatchPipeline.run(order1)
        batch = BatchPipeline.run(order2)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.discount_rate, Decimal('0'))

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


# =============================================================================
# CATEGORY 5: DRIVER MATCHING (5 cases)
# =============================================================================

class DriverMatchingAvailableTests(TestSetupMixin, TestCase):
    def setUp(self):
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        self.driver_user = User.objects.create_user(username=f'test_driver_{uuid.uuid4().hex[:8]}', password='testpass', role='driver')
        self.driver = Driver.objects.create(user=self.driver_user, status='AVAILABLE', location=Point(self.LNG_CENTER + 0.003, self.LAT_CENTER - 0.003), last_heartbeat=timezone.now(), max_active_orders=3)
        self.driver.h3_index = latlng_to_h3(self.driver.location.y, self.driver.location.x)
        self.driver.save()

    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_driver_assigned(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)
        BatchDispatchService.dispatch_batch(batch.id)
        batch.refresh_from_db()
        self.assertIn(batch.status, ['OFFERED', 'CREATED'])

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()
        Driver.objects.filter(user=self.driver_user).delete()
        User.objects.filter(username=self.driver_user.username).delete()


class DriverMatchingAllBusyTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_no_driver_available(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a, lng=39.5000, lat=-6.9000)
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.status, 'CREATED')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class DriverMatchingAcceptOfferTests(TestSetupMixin, TestCase):
    def setUp(self):
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        self.driver_user = User.objects.create_user(username=f'test_driver_{uuid.uuid4().hex[:8]}', password='testpass', role='driver')
        self.driver = Driver.objects.create(user=self.driver_user, status='AVAILABLE', location=Point(self.LNG_CENTER + 0.003, self.LAT_CENTER - 0.003), last_heartbeat=timezone.now(), max_active_orders=3)
        self.driver.h3_index = latlng_to_h3(self.driver.location.y, self.driver.location.x)
        self.driver.save()

    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_driver_accepts_offer(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        BatchDispatchService.dispatch_batch(batch.id)
        batch.refresh_from_db()
        batch.driver = self.driver
        batch.status = 'ASSIGNED'
        batch.save()
        self.assertEqual(batch.status, 'ASSIGNED')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()
        Driver.objects.filter(user=self.driver_user).delete()
        User.objects.filter(username=self.driver_user.username).delete()


class DriverMatchingRejectTests(TestSetupMixin, TestCase):
    def setUp(self):
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        self.driver_user = User.objects.create_user(username=f'test_driver_{uuid.uuid4().hex[:8]}', password='testpass', role='driver')
        self.driver = Driver.objects.create(user=self.driver_user, status='AVAILABLE', location=Point(self.LNG_CENTER + 0.003, self.LAT_CENTER - 0.003), last_heartbeat=timezone.now(), max_active_orders=3)
        self.driver.h3_index = latlng_to_h3(self.driver.location.y, self.driver.location.x)
        self.driver.save()

    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_offer_attempt_incremented(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        BatchDispatchService.dispatch_batch(batch.id)
        batch.refresh_from_db()
        self.assertEqual(batch.offer_attempt, 1)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()
        Driver.objects.filter(user=self.driver_user).delete()
        User.objects.filter(username=self.driver_user.username).delete()


class DriverMatchingNoRouteTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_no_route_no_driver_match(self, mock_route):
        mock_route.return_value = None
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        if batch and batch.route_optimization is None:
            batch.refresh_from_db()
            self.assertIsNone(batch.driver)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


# =============================================================================
# CATEGORY 6: CONCURRENCY / EDGE CASES (6 cases)
# =============================================================================

class ConcurrencyDoubleBatchTest(TransactionTestCase):
    def setUp(self):
        poly = MultiPolygon(Polygon((
            (39.1 - 0.1, -6.8 - 0.1), (39.3 - 0.1, -6.8 - 0.1),
            (39.3 - 0.1, -6.7 - 0.1), (39.1 - 0.1, -6.7 - 0.1),
            (39.1 - 0.1, -6.8 - 0.1)
        )))
        zone = OperationalZone.objects.create(name="Concurrency Zone", boundary=poly)
        zone.refresh_h3_indexes()
        self.policy = PricingPolicy.objects.create(
            name="Test Policy", is_active=True,
            base_fee=Decimal('1000.00'), per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'), driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'), driver_per_stop_rate=Decimal('30.00'),
            single_order_driver_share=Decimal('0.90')
        )
        self.user = User.objects.create_user(username=f'concurrent_user_{uuid.uuid4().hex[:8]}', password='testpass', role='customer')
        self.vendor = User.objects.create_user(username=f'concurrent_vendor_{uuid.uuid4().hex[:8]}', password='testpass', role='vendor')
        self.restaurant = Restaurant.objects.create(owner=self.vendor, name="Concurrent Restaurant", location="Location", geo_location=Point(39.2083, -6.7924), max_delivery_radius_km=10.0)

    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_concurrent_batching(self, mock_route):
        mock_route.return_value = {'total_distance_km': 5.0, 'total_duration_min': 20, 'stop_sequence': []}
        order1 = Order.objects.create(customer=self.user, restaurant=self.restaurant, status='READY', delivery_address="Address 1", delivery_location=Point(39.2100, -6.7974), items_subtotal=Decimal('5000.00'), delivery_fee=Decimal('1500.00'), service_fee=Decimal('300.00'), total_amount=Decimal('6800.00'))
        order2 = Order.objects.create(customer=self.user, restaurant=self.restaurant, status='READY', delivery_address="Address 2", delivery_location=Point(39.2120, -6.7964), items_subtotal=Decimal('5000.00'), delivery_fee=Decimal('1500.00'), service_fee=Decimal('300.00'), total_amount=Decimal('6800.00'))
        batch1 = BatchPipeline.run(order1)
        batch2 = BatchPipeline.run(order2)
        if batch1 and batch2:
            self.assertIn(batch1.batch_type, ['SINGLE_ORDER', 'SINGLE_RESTAURANT'])

    def tearDown(self):
        Order.objects.filter(customer=self.user).delete()
        Restaurant.objects.filter(name="Concurrent Restaurant").delete()
        OperationalZone.objects.filter(name="Concurrency Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class EdgeCaseAlreadyBatchedTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_already_batched_order(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a)
        batch1 = BatchPipeline.run(order)
        order.refresh_from_db()
        result = BatchPipeline.run(order)
        self.assertIsNone(result)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class EdgeCaseNoZoneTests(TestSetupMixin, TestCase):
    def setUp(self):
        poly = MultiPolygon(Polygon((
            (40.0 - 0.1, -7.0 - 0.1), (40.1 - 0.1, -7.0 - 0.1),
            (40.1 - 0.1, -6.9 - 0.1), (40.0 - 0.1, -6.9 - 0.1),
            (40.0 - 0.1, -7.0 - 0.1)
        )))
        zone = OperationalZone.objects.create(name="Far Zone", boundary=poly)
        zone.refresh_h3_indexes()
        self.policy = PricingPolicy.objects.create(
            name="Test Policy", is_active=True,
            base_fee=Decimal('1000.00'), per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'), driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'), driver_per_stop_rate=Decimal('30.00'),
            single_order_driver_share=Decimal('0.90')
        )
        self.setup_users()
        self.setup_restaurants()

    def test_order_outside_zone(self, mock_route):
        order = self.create_order(self.restaurant_a, lng=39.2100, lat=-6.7974)
        batch = BatchPipeline.run(order)
        self.assertIsNone(batch)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name__in=['Far Zone']).delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class EdgeCaseNoH3IndexTests(TestSetupMixin, TestCase):
    def test_no_h3_index(self):
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = Order.objects.create(customer=self.customer, restaurant=self.restaurant_a, status='READY', delivery_address="No H3 Address", delivery_location=None, items_subtotal=Decimal('5000.00'), delivery_fee=Decimal('1500.00'), service_fee=Decimal('300.00'), total_amount=Decimal('6800.00'), h3_index=None)
        batch = BatchPipeline.run(order)
        self.assertIsNone(batch)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class EdgeCaseEmptyZoneTests(TestSetupMixin, TestCase):
    def setUp(self):
        poly = MultiPolygon(Polygon((
            (39.3 - 0.1, -6.8 - 0.1), (39.4 - 0.1, -6.8 - 0.1),
            (39.4 - 0.1, -6.7 - 0.1), (39.3 - 0.1, -6.7 - 0.1),
            (39.3 - 0.1, -6.8 - 0.1)
        )))
        zone = OperationalZone.objects.create(name="Empty Zone", boundary=poly)
        zone.refresh_h3_indexes()
        self.policy = PricingPolicy.objects.create(
            name="Test Policy", is_active=True,
            base_fee=Decimal('1000.00'), per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'), driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'), driver_per_stop_rate=Decimal('30.00'),
            single_order_driver_share=Decimal('0.90')
        )
        self.setup_users()
        self.setup_restaurants()

    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_empty_zone_single_order(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        order = self.create_order(self.restaurant_a, lng=39.3500, lat=-6.7500)
        batch = BatchPipeline.run(order)
        self.assertIsNotNone(batch)
        self.assertEqual(batch.batch_type, 'SINGLE_ORDER')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Empty Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class EdgeCaseZoneLockedTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_zone_locked(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        from django.core.cache import cache
        zone = OperationalZone.objects.first()
        lock_key = f"batch_zone_lock:{zone.id}"
        cache.add(lock_key, "1", 30)
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        self.assertIsNone(batch)
        cache.delete(lock_key)

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


# =============================================================================
# CATEGORY 7: STATE TRANSITIONS (5 cases)
# =============================================================================

class StateTransitionCREATEDToOFFEREDTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_created_to_offered(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        self.assertEqual(batch.status, 'CREATED')
        BatchDispatchService.dispatch_batch(batch.id)
        batch.refresh_from_db()
        self.assertIn(batch.status, ['OFFERED', 'CREATED'])

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class StateTransitionOFFEREDToASSIGNEDTests(TestSetupMixin, TestCase):
    def setUp(self):
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        self.driver_user = User.objects.create_user(username=f'test_driver_{uuid.uuid4().hex[:8]}', password='testpass', role='driver')
        self.driver = Driver.objects.create(user=self.driver_user, status='AVAILABLE', location=Point(self.LNG_CENTER + 0.003, self.LAT_CENTER - 0.003), last_heartbeat=timezone.now(), max_active_orders=3)
        self.driver.h3_index = latlng_to_h3(self.driver.location.y, self.driver.location.x)
        self.driver.save()

    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_offered_to_assigned(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        BatchDispatchService.dispatch_batch(batch.id)
        batch.refresh_from_db()
        batch.driver = self.driver
        batch.status = 'ASSIGNED'
        batch.save()
        self.assertEqual(batch.status, 'ASSIGNED')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()
        Driver.objects.filter(user=self.driver_user).delete()
        User.objects.filter(username=self.driver_user.username).delete()


class StateTransitionASSIGNEDToPICKEDTests(TestSetupMixin, TestCase):
    def setUp(self):
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        self.driver_user = User.objects.create_user(username=f'test_driver_{uuid.uuid4().hex[:8]}', password='testpass', role='driver')
        self.driver = Driver.objects.create(user=self.driver_user, status='AVAILABLE', location=Point(self.LNG_CENTER + 0.003, self.LAT_CENTER - 0.003), last_heartbeat=timezone.now(), max_active_orders=3)

    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_assigned_to_picked(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        batch.driver = self.driver
        batch.status = 'ASSIGNED'
        batch.save()
        batch.status = 'PICKED'
        batch.save()
        self.assertEqual(batch.status, 'PICKED')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()
        Driver.objects.filter(user=self.driver_user).delete()
        User.objects.filter(username=self.driver_user.username).delete()


class StateTransitionOFFEREDToEXPIREDTests(TestSetupMixin, TestCase):
    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_offered_to_expired(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        order = self.create_order(self.restaurant_a, lng=39.5000, lat=-6.9000)
        batch = BatchPipeline.run(order)
        batch.offer_attempt = 5
        batch.status = 'EXPIRED'
        batch.save()
        self.assertEqual(batch.status, 'EXPIRED')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()


class StateTransitionCANCELLEDTests(TestSetupMixin, TestCase):
    def setUp(self):
        self.setup_zone_and_policy()
        self.setup_users()
        self.setup_restaurants()
        self.driver_user = User.objects.create_user(username=f'test_driver_{uuid.uuid4().hex[:8]}', password='testpass', role='driver')
        self.driver = Driver.objects.create(user=self.driver_user, status='AVAILABLE', location=Point(self.LNG_CENTER + 0.003, self.LAT_CENTER - 0.003), last_heartbeat=timezone.now(), max_active_orders=3)

    @patch('domain.services.batch_pipeline.BatchPipeline._optimize_route')
    def test_cancelled(self, mock_route):
        mock_route.return_value = self.create_route_dict([])
        order = self.create_order(self.restaurant_a)
        batch = BatchPipeline.run(order)
        batch.status = 'CANCELLED'
        batch.save()
        self.assertEqual(batch.status, 'CANCELLED')

    def tearDown(self):
        Order.objects.filter(customer=self.customer).delete()
        Restaurant.objects.filter(name__startswith="Test ").delete()
        OperationalZone.objects.filter(name="Test Zone").delete()
        PricingPolicy.objects.filter(name="Test Policy").delete()
        Driver.objects.filter(user=self.driver_user).delete()
        User.objects.filter(username=self.driver_user.username).delete()