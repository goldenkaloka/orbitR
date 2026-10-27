"""
A nearby-bucket push is queued when the bucket is created, so it can run after
the bucket is cancelled or has expired. Pushing then sends people to a bucket
that no longer exists, which is what customers reported seeing.
"""

from datetime import timedelta
from unittest.mock import patch

from apps.delivery.tasks import notify_nearby_pool_created
from apps.orders.models import OrderPool
from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone

POINT = Point(39.2083, -6.7924)


class NearbyPoolNotifyTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(
            username='owner_notify', phone='+255700001050',
            password='password123', role='vendor',
        )
        self.initiator = User.objects.create_user(
            username='notify_host', phone='+255700001051',
            password='password123', role='customer',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Notify Diner',
            location='Main Hall',
            geo_location=POINT,
        )

    def _pool(self, **overrides):
        defaults = {
            'initiator': self.initiator,
            'restaurant': self.restaurant,
            'invite_code': 'NOTIFY',
            'delivery_address': '12 Notify Road',
            'delivery_location': POINT,
            'expires_at': timezone.now() + timedelta(hours=2),
        }
        defaults.update(overrides)
        return OrderPool.objects.create(**defaults)

    @patch('infrastructure.notifications.service.NotificationService.send_push')
    def test_cancelled_pool_is_not_announced(self, send_push):
        pool = self._pool(status='CANCELLED')

        result = notify_nearby_pool_created(str(pool.id), self.restaurant.name)

        self.assertIn('not open', result)
        send_push.assert_not_called()

    @patch('infrastructure.notifications.service.NotificationService.send_push')
    def test_expired_pool_is_not_announced(self, send_push):
        pool = self._pool(expires_at=timezone.now() - timedelta(minutes=1))

        result = notify_nearby_pool_created(str(pool.id), self.restaurant.name)

        self.assertIn('not open', result)
        send_push.assert_not_called()

    @patch('infrastructure.notifications.service.NotificationService.send_push')
    def test_open_pool_is_still_announced(self, send_push):
        pool = self._pool()

        result = notify_nearby_pool_created(str(pool.id), self.restaurant.name)

        # Nobody else is active nearby, so the task finds no audience — but it
        # must not take the "gone" shortcut.
        self.assertIn('No users to notify', result)
        send_push.assert_not_called()
