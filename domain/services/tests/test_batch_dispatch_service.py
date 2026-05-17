from django.test import TestCase
from django.contrib.gis.geos import Point
from django.utils import timezone
from decimal import Decimal
from unittest.mock import patch, MagicMock
from datetime import timedelta

from apps.restaurants.models import Restaurant
from apps.pricing.models import PricingPolicy
from apps.orders.models import Order, DeliveryBatch
from apps.delivery.models import Driver
from apps.users.models import User
from domain.services.batch_dispatch_service import BatchDispatchService, MAX_OFFER_ATTEMPTS, OFFER_EXPIRY_SECONDS
from domain.events.bus import EventBus


class BatchDispatchServiceTests(TestCase):
    """Unit tests for BatchDispatchService"""

    def setUp(self):
        self.user = User.objects.create_user(username='dispatch_test_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Dispatch Test Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )

        self.policy = PricingPolicy.objects.create(
            name="Dispatch Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            driver_min_guarantee=Decimal('200.00'),
            driver_per_km_rate=Decimal('50.00'),
            driver_per_stop_rate=Decimal('30.00')
        )

        self.driver = Driver.objects.create(
            user=self.user,
            name="Test Driver",
            phone="+255700000000",
            status='AVAILABLE',
            driver_type='permanent',
            location=Point(39.2026, -6.7924),
            h3_index='892834b0c9fffff',
            rating=Decimal('4.50'),
            current_active_orders=0
        )

    def test_batch_dispatch_creates_offer(self):
        """Test that dispatch_batch creates offer"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00'),
            company_profit=Decimal('300.00'),
            offer_attempt=0
        )

        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            driver=self.driver,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='READY',
            delivery_fee=Decimal('2000.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('5000.00'),
            total_amount=Decimal('7300.00'),
            batch=batch
        )

        with patch('domain.services.cross_batching_service.CrossBatchDriverMatcher.find_best_driver_for_batch') as mock_find:
            mock_find.return_value = self.driver

            result = BatchDispatchService.dispatch_batch(str(batch.id))

            self.assertIsNotNone(result)
            self.assertEqual(result.status, 'OFFERED')

    def test_offer_to_driver_updates_batch_status(self):
        """Test that offer_to_driver updates batch status to OFFERED"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_RESTAURANT',
            status='CREATED',
            user_paid_total=Decimal('3000.00'),
            driver_payout=Decimal('2500.00'),
            company_profit=Decimal('500.00'),
            offer_attempt=0
        )

        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='READY',
            batch=batch
        )

        BatchDispatchService._offer_to_driver(batch, self.driver)

        batch.refresh_from_db()
        self.assertEqual(batch.status, 'OFFERED')
        self.assertEqual(batch.driver, self.driver)
        self.assertEqual(batch.offer_attempt, 1)

    def test_accept_batch_updates_order_status(self):
        """Test that accept_batch updates batch and order statuses"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='OFFERED',
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00'),
            company_profit=Decimal('300.00'),
            driver=self.driver,
            offer_attempt=1
        )

        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            driver=self.driver,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='OFFERED',
            batch=batch
        )

        BatchDispatchService.accept_batch(str(batch.id), str(self.driver.id), self.user)

        batch.refresh_from_db()
        order.refresh_from_db()

        self.assertEqual(batch.status, 'ASSIGNED')
        self.assertEqual(order.status, 'ASSIGNED')

    def test_decline_batch_resets_status(self):
        """Test that decline_batch resets batch and order statuses"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='OFFERED',
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00'),
            company_profit=Decimal('300.00'),
            driver=self.driver,
            offer_attempt=1
        )

        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            driver=self.driver,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='OFFERED',
            batch=batch
        )

        BatchDispatchService.decline_batch(str(batch.id), str(self.driver.id), self.user)

        batch.refresh_from_db()
        order.refresh_from_db()

        self.assertEqual(batch.status, 'CREATED')
        self.assertEqual(batch.driver, None)
        self.assertEqual(order.status, 'READY')
        self.assertEqual(order.driver, None)

    def test_max_attempts_reached_marks_expired(self):
        """Test that batch reaches expired after max offer attempts"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00'),
            company_profit=Decimal('300.00'),
            offer_attempt=MAX_OFFER_ATTEMPTS
        )

        order = Order.objects.create(
            restaurant=self.restaurant,
            customer=self.user,
            delivery_location=Point(39.2100, -6.7924),
            delivery_address="Test Address",
            status='READY',
            batch=batch
        )

        with patch('domain.services.batch_dispatch_service.BatchDispatchService._find_best_driver') as mock_find:
            mock_find.return_value = None

            BatchDispatchService.dispatch_batch(str(batch.id))

            batch.refresh_from_db()
            self.assertEqual(batch.status, 'EXPIRED')

    def test_schedule_retry_schedules_task(self):
        """Test that _schedule_retry schedules a retry task"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            offer_attempt=1
        )

        with patch('domain.services.dispatch_tasks.retry_batch_dispatch_task.apply_async') as mock_task:
            BatchDispatchService._schedule_retry(batch.id, 2)
            mock_task.assert_called_once()

    def test_dispatch_batch_returns_none_for_non_created_status(self):
        """Test that dispatch returns None for non-CREATED status"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='ASSIGNED',
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00'),
            company_profit=Decimal('300.00')
        )

        result = BatchDispatchService.dispatch_batch(str(batch.id))

        self.assertIsNone(result)

    def test_dispatch_batch_returns_none_for_max_attempts(self):
        """Test that dispatch returns None when max attempts reached"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00'),
            company_profit=Decimal('300.00'),
            offer_attempt=MAX_OFFER_ATTEMPTS
        )

        with patch('domain.services.batch_dispatch_service.cache') as mock_cache:
            mock_cache.add.return_value = True

            result = BatchDispatchService.dispatch_batch(str(batch.id))

            self.assertIsNone(result)
            batch.refresh_from_db()
            self.assertEqual(batch.status, 'CREATED')


class BatchDispatchServiceDriverTests(TestCase):
    """Tests for driver handling in batch dispatch"""

    def setUp(self):
        self.user = User.objects.create_user(username='driver_test_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Driver Test Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )

        self.driver = Driver.objects.create(
            user=self.user,
            name="Available Driver",
            phone="+255700000000",
            status='AVAILABLE',
            driver_type='permanent',
            location=Point(39.2026, -6.7924),
            h3_index='892834b0c9fffff',
            rating=Decimal('4.50'),
            current_active_orders=0
        )

    def test_find_best_driver_calls_matcher(self):
        """Test that _find_best_driver uses CrossBatchDriverMatcher"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00'),
            company_profit=Decimal('300.00')
        )

        with patch('domain.services.cross_batching_service.CrossBatchDriverMatcher.find_best_driver_for_batch') as mock_matcher:
            mock_matcher.return_value = self.driver

            driver = BatchDispatchService._find_best_driver(batch)

            mock_matcher.assert_called_once_with(batch, max_radius_km=15)
            self.assertEqual(driver, self.driver)

    def test_find_best_driver_returns_none_when_no_driver(self):
        """Test that _find_best_driver returns None when no driver found"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00'),
            company_profit=Decimal('300.00')
        )

        with patch('domain.services.cross_batching_service.CrossBatchDriverMatcher.find_best_driver_for_batch') as mock_matcher:
            mock_matcher.return_value = None

            driver = BatchDispatchService._find_best_driver(batch)

            self.assertIsNone(driver)


class BatchDispatchServiceConcurrencyTests(TestCase):
    """Tests for concurrent access handling"""

    def setUp(self):
        self.user = User.objects.create_user(username='concurrent_test_user', password='testpass')
        self.restaurant = Restaurant.objects.create(
            name="Concurrent Test Restaurant",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924),
            max_delivery_radius_km=10.0
        )

    def test_dispatch_batch_prevents_duplicate_dispatch(self):
        """Test that concurrent dispatch is prevented by cache lock"""
        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            user_paid_total=Decimal('2300.00'),
            driver_payout=Decimal('2000.00'),
            company_profit=Decimal('300.00'),
            offer_attempt=0
        )

        with patch('domain.services.batch_dispatch_service.cache') as mock_cache:
            mock_cache.add.return_value = False

            result = BatchDispatchService.dispatch_batch(str(batch.id))

            self.assertIsNone(result)