from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.test import TestCase


class RestaurantModelTests(TestCase):
    def test_restaurant_string_uses_name(self):
        owner = User.objects.create_user(
            phone="+255670000002",
            username="vendor",
            password="pass",
            role="vendor",
        )
        restaurant = Restaurant.objects.create(
            owner=owner,
            name="Campus Bites",
            location="UDSM",
        )

        self.assertEqual(str(restaurant), "Campus Bites")
