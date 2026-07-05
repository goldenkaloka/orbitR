from apps.delivery.models import Driver
from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.test import TestCase


class RestaurantVisibleDriversTests(TestCase):
    """Tests for restaurant_visible_drivers with 2 driver types (platform + restaurant)."""

    def setUp(self):
        self.owner = User.objects.create_user(username='rest_owner', password='password123', role='vendor')
        self.restaurant = Restaurant.objects.create(
            name="Pizza Place",
            owner=self.owner,
        )

    def test_visible_drivers_includes_both_types(self):
        """Both platform drivers and restaurant-specific drivers are visible."""
        from domain.services.permissions import restaurant_visible_drivers

        platform_user = User.objects.create_user(username='rider_plat', password='password123', role='driver')
        platform_driver = Driver.objects.create(
            user=platform_user,
            driver_type='platform',
            status='OFFLINE',
            priority=5,
        )

        rest_user = User.objects.create_user(username='rider_rest', password='password123', role='driver')
        rest_driver = Driver.objects.create(
            user=rest_user,
            driver_type='restaurant',
            home_restaurant=self.restaurant,
            status='OFFLINE',
            priority=1,
        )

        # A driver for a different restaurant should NOT be visible
        other_rest = Restaurant.objects.create(name="Other Place", owner=self.owner)
        other_user = User.objects.create_user(username='rider_other', password='password123', role='driver')
        Driver.objects.create(
            user=other_user,
            driver_type='restaurant',
            home_restaurant=other_rest,
            status='OFFLINE',
        )

        drivers = list(restaurant_visible_drivers(self.restaurant))
        driver_ids = [d.id for d in drivers]

        self.assertIn(platform_driver.id, driver_ids)
        self.assertIn(rest_driver.id, driver_ids)
        self.assertNotIn(other_user.driver_profile.id, driver_ids)

    def test_visible_drivers_sorted_by_priority(self):
        """Platform drivers sorted by -priority, then current_active_orders."""
        from domain.services.permissions import restaurant_visible_drivers

        low = User.objects.create_user(username='rider_low', password='password123', role='driver')
        Driver.objects.create(user=low, driver_type='platform', priority=1)

        high = User.objects.create_user(username='rider_high', password='password123', role='driver')
        Driver.objects.create(user=high, driver_type='platform', priority=10)

        drivers = list(restaurant_visible_drivers(self.restaurant))
        self.assertEqual(drivers[0].id, high.driver_profile.id)
        self.assertEqual(drivers[1].id, low.driver_profile.id)
