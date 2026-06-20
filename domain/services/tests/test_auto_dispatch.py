from apps.delivery.models import Driver
from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase


class RestaurantVisibleDriversTests(TestCase):
    """Tests for restaurant_visible_drivers proximity sorting"""

    def setUp(self):
        self.owner = User.objects.create_user(username='rest_owner', password='password123', role='vendor')
        self.restaurant = Restaurant.objects.create(
            name="Pizza Place",
            owner=self.owner,
            geo_location=Point(39.2083, -6.7924)
        )

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
