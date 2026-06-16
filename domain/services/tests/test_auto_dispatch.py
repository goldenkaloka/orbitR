from django.test import TestCase
from django.contrib.gis.geos import Point
from django.utils import timezone
from datetime import timedelta
from decimal import Decimal

from apps.restaurants.models import Restaurant
from apps.users.models import User
from apps.orders.models import Order, DeliveryBatch
from apps.delivery.models import Driver
from domain.services.dispatch_tasks import auto_assign_pending_batches_task


class AutoAssignPendingBatchesTests(TestCase):
    """Unit tests for auto_assign_pending_batches_task"""

    def setUp(self):
        self.owner = User.objects.create_user(username='rest_owner', password='password123', role='vendor')
        self.restaurant = Restaurant.objects.create(
            name="Pizza Place",
            owner=self.owner,
            geo_location=Point(39.2083, -6.7924)
        )

        self.driver_user = User.objects.create_user(username='rider1', password='password123', role='driver')
        self.driver = Driver.objects.create(
            user=self.driver_user,
            status='AVAILABLE',
            serves_all=True,  # platform driver
            location=Point(39.2083, -6.7924),
            max_active_orders=3
        )

        self.customer = User.objects.create_user(username='cust1', password='password123', role='customer')

    def test_auto_assign_pending_batch_after_timeout(self):
        """Test that a batch in PENDING_DRIVER for > 3 minutes is auto-assigned"""
        batch = DeliveryBatch.objects.create(
            restaurant=self.restaurant,
            status='PENDING_DRIVER',
            waiting_since=timezone.now() - timedelta(minutes=4)
        )
        
        # Create a ready order inside the batch to pass validation checks
        Order.objects.create(
            restaurant=self.restaurant,
            customer=self.customer,
            status='READY',
            batch=batch,
            delivery_location=Point(39.2083, -6.7924),
            delivery_fee=Decimal('1000.00'),
            service_fee=Decimal('300.00'),
            items_subtotal=Decimal('5000.00'),
            total_amount=Decimal('6300.00')
        )

        auto_assign_pending_batches_task()

        batch.refresh_from_db()
        self.assertEqual(batch.status, 'DISPATCHED')
        self.assertEqual(batch.driver, self.driver)

    def test_no_auto_assign_before_timeout(self):
        """Test that a batch in PENDING_DRIVER for < 3 minutes is NOT auto-assigned"""
        batch = DeliveryBatch.objects.create(
            restaurant=self.restaurant,
            status='PENDING_DRIVER',
            waiting_since=timezone.now() - timedelta(minutes=1)
        )
        
        Order.objects.create(
            restaurant=self.restaurant,
            customer=self.customer,
            status='READY',
            batch=batch,
            delivery_location=Point(39.2083, -6.7924)
        )

        auto_assign_pending_batches_task()

        batch.refresh_from_db()
        self.assertEqual(batch.status, 'PENDING_DRIVER')
        self.assertIsNone(batch.driver)

    def test_restaurant_visible_drivers_proximity_sorting(self):
        """Test that restaurant_visible_drivers sorts nearby platform drivers first"""
        from domain.services.permissions import restaurant_visible_drivers

        # The restaurant has a valid H3 index set on creation
        self.assertIsNotNone(self.restaurant.h3_index)

        # Create a nearby driver (in the same hexagon) with priority 5
        nearby_driver_user = User.objects.create_user(username='rider_near', password='password123', role='driver')
        nearby_driver = Driver.objects.create(
            user=nearby_driver_user,
            status='AVAILABLE',
            serves_all=True,
            location=Point(39.2083, -6.7924),
            priority=5
        )

        # Create a far driver (in a different location/index) with a higher priority 10
        far_driver_user = User.objects.create_user(username='rider_far', password='password123', role='driver')
        far_driver = Driver.objects.create(
            user=far_driver_user,
            status='AVAILABLE',
            serves_all=True,
            location=Point(40.2083, -6.7924),  # far away
            priority=10
        )

        # Retrieve visible drivers
        drivers = list(restaurant_visible_drivers(self.restaurant))

        # Check that nearby_driver is first in the list despite having lower priority
        # than far_driver because nearby_driver is in the restaurant's neighboring H3 cell.
        self.assertEqual(drivers[0].id, nearby_driver.id)
