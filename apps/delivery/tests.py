from django.test import TestCase
from django.contrib.auth import get_user_model
from apps.orders.models import Order
from apps.delivery.models import Driver
from apps.restaurants.models import Restaurant
from domain.services.order_services import DispatchService, AssignDriverService
from django.contrib.gis.geos import Point

User = get_user_model()

class DeliverySystemTest(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(username='customer', password='pass', role='customer')
        self.vendor_user = User.objects.create_user(username='vendor', password='pass', role='vendor')
        self.driver_user = User.objects.create_user(username='driver', password='pass', role='driver')
        
        self.restaurant = Restaurant.objects.create(
            owner=self.vendor_user,
            name="Test Kitchen",
            geo_location=Point(39.2, -6.8, srid=4326)
        )
        
        self.driver = Driver.objects.create(
            user=self.driver_user,
            status='AVAILABLE',
            location=Point(39.201, -6.801, srid=4326)
        )

        
        self.order = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='READY',
            delivery_location=Point(39.205, -6.805, srid=4326)
        )

    def test_manual_assignment(self):
        admin_user = User.objects.create_superuser(username='admin', password='pass', email='admin@test.com')
        AssignDriverService.execute(self.order.id, self.driver.id, admin_user)
        self.order.refresh_from_db()
        self.assertEqual(self.order.driver, self.driver)
        self.assertEqual(self.order.status, 'ASSIGNED')

    def test_automated_dispatch_success(self):
        # Driver is within 15km (very close)
        DispatchService.execute(self.order.id)
        self.order.refresh_from_db()
        self.assertEqual(self.order.driver, self.driver)
        self.assertEqual(self.order.status, 'ASSIGNED')

    def test_dispatch_failure_no_drivers(self):
        # Move driver far away
        self.driver.location = Point(40.5, -7.5, srid=4326)
        self.driver.save()
        
        DispatchService.execute(self.order.id)
        self.order.refresh_from_db()
        self.assertIsNone(self.order.driver)
        self.assertEqual(self.order.status, 'READY') # Stays ready
