from django.contrib.auth import get_user_model
from django.contrib.gis.geos import Point
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from api.serializers.reviews import ReviewSerializer
from apps.delivery.models import Driver
from apps.orders.models import Order
from apps.restaurants.models import Restaurant


User = get_user_model()


class ReviewIntegrityTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.customer = User.objects.create_user(username='customer', phone='0712000001', password='pass')
        self.vendor = User.objects.create_user(username='vendor', phone='0712000002', password='pass', role='vendor')
        self.other_vendor = User.objects.create_user(username='other-vendor', phone='0712000003', password='pass', role='vendor')
        self.driver_user = User.objects.create_user(username='driver', phone='0712000004', password='pass', role='driver')
        self.restaurant = Restaurant.objects.create(owner=self.vendor, name='A', geo_location=Point(39.2, -6.8, srid=4326))
        self.other_restaurant = Restaurant.objects.create(owner=self.other_vendor, name='B', geo_location=Point(39.3, -6.9, srid=4326))
        self.driver = Driver.objects.create(user=self.driver_user)
        self.order = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            driver=self.driver,
            status='DELIVERED',
            is_paid=True,
        )

    def _request(self):
        request = self.factory.post('/api/reviews/')
        request.user = self.customer
        return request

    def test_restaurant_review_must_match_delivered_order_restaurant(self):
        serializer = ReviewSerializer(
            data={
                'review_type': 'restaurant',
                'order': str(self.order.id),
                'restaurant': str(self.other_restaurant.id),
                'rating': 5,
            },
            context={'request': self._request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('Restaurant review target must match', str(serializer.errors))

    def test_driver_review_must_match_delivered_order_driver(self):
        other_driver_user = User.objects.create_user(username='driver2', phone='0712000005', password='pass', role='driver')
        other_driver = Driver.objects.create(user=other_driver_user)
        serializer = ReviewSerializer(
            data={
                'review_type': 'driver',
                'order': str(self.order.id),
                'driver': str(other_driver.id),
                'rating': 5,
            },
            context={'request': self._request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn('Driver review target must match', str(serializer.errors))

    def test_valid_order_review_is_verified_purchase(self):
        serializer = ReviewSerializer(
            data={
                'review_type': 'restaurant',
                'order': str(self.order.id),
                'restaurant': str(self.restaurant.id),
                'rating': 5,
            },
            context={'request': self._request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        review = serializer.save(customer=self.customer)
        self.assertTrue(review.is_verified_purchase)
        self.assertTrue(review.is_visible)
        self.assertEqual(review.moderation_status, 'APPROVED')
