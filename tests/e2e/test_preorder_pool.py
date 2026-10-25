"""
Group pre-orders and kitchen slot capacity.

A bucket books one seat in its eating-time slot from creation until it is
finished or aborted, no matter how many members join. The orders the bucket
later spawns must not consume a second seat.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from apps.orders.models import Order, OrderPool, PoolMember
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from domain.services.orders.order_pooling import PoolOrderService
from domain.services.orders.scheduled_orders import PreorderSlotUnavailable
from rest_framework.test import APIClient

POINT = Point(39.2083, -6.7924)
ADDRESS = '12 Slots Road'


def _parse(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


class PreorderPoolTests(TestCase):
    def setUp(self):
        self.initiator = User.objects.create_user(
            username='pool_host', phone='+255700001040',
            password='password123', role='customer',
        )
        self.friend = User.objects.create_user(
            username='pool_friend', phone='+255700001041',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='owner_pool_pre', phone='+255700001042',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Bucket Diner',
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=25,
            preorder_slot_capacity=2,
        )
        section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        Food.objects.create(
            section=section, name='Bucket Plate', price=Decimal('10000.00'),
        )
        self.client = APIClient()

        for target in (
            'domain.services.orders.order_pooling.notify_nearby_pool_created',
            'domain.services.orders.order_pooling.broadcast_pool_status',
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _slots(self):
        response = self.client.get(
            f'/api/restaurants/{self.restaurant.id}/preorder-slots/?days=2'
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _first_slot(self):
        for day in self._slots()['days']:
            for slot in day['slots']:
                if slot['available']:
                    return slot
        self.fail('API returned no bookable slots')

    def _slot(self, start):
        for day in self._slots()['days']:
            for slot in day['slots']:
                if slot['start'] == start:
                    return slot
        self.fail(f'Slot {start} is not listed')

    def _create_pool(self, user, scheduled_for=None):
        return PoolOrderService.create_pool(
            initiator=user,
            restaurant_id=self.restaurant.id,
            delivery_address=ADDRESS,
            delivery_location=POINT,
            scheduled_for=scheduled_for,
        )

    def _set_capacity(self, capacity):
        self.restaurant.preorder_slot_capacity = capacity
        self.restaurant.save(update_fields=['preorder_slot_capacity'])

    def test_bucket_reserves_one_seat_regardless_of_member_count(self):
        slot = self._first_slot()
        start = _parse(slot['start'])

        pool = self._create_pool(self.initiator, start)
        PoolMember.objects.create(pool=pool, user=self.friend, status='ADDING')

        self.assertEqual(pool.scheduled_for, start)
        self.assertEqual(self._slot(slot['start'])['remaining'], slot['remaining'] - 1)

    def test_member_orders_do_not_consume_a_second_seat(self):
        slot = self._first_slot()
        start = _parse(slot['start'])
        pool = self._create_pool(self.initiator, start)
        after_pool = self._slot(slot['start'])['remaining']

        Order.objects.create(
            customer=self.initiator,
            restaurant=self.restaurant,
            pool=pool,
            is_pool_order=True,
            is_paid=True,
            status='SCHEDULED',
            scheduled_for=start,
            items_subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
        )

        self.assertEqual(self._slot(slot['start'])['remaining'], after_pool)

    def test_full_slot_rejects_a_second_bucket(self):
        self._set_capacity(1)
        slot = self._first_slot()
        start = _parse(slot['start'])
        self._create_pool(self.initiator, start)

        with self.assertRaisesMessage(PreorderSlotUnavailable, 'fully booked'):
            self._create_pool(self.friend, start)

        self.assertEqual(self._slot(slot['start'])['remaining'], 0)
        self.assertEqual(OrderPool.objects.count(), 1)

    def test_bucket_requires_lead_time_and_horizon(self):
        with self.assertRaisesMessage(ValueError, 'at least'):
            self._create_pool(
                self.initiator, timezone.now() + timedelta(minutes=10),
            )
        with self.assertRaisesMessage(ValueError, 'at most'):
            self._create_pool(
                self.initiator, timezone.now() + timedelta(days=30),
            )
        self.assertFalse(OrderPool.objects.exists())

    def test_immediate_bucket_does_not_consume_a_preorder_seat(self):
        slot = self._first_slot()

        pool = self._create_pool(self.initiator)

        self.assertIsNone(pool.scheduled_for)
        self.assertEqual(pool.pool_type, 'IMMEDIATE')
        self.assertEqual(self._slot(slot['start'])['remaining'], slot['remaining'])

    def test_scheduled_bucket_expires_when_the_kitchen_must_start(self):
        slot = self._first_slot()
        start = _parse(slot['start'])

        pool = self._create_pool(self.initiator, start)

        self.assertEqual(
            pool.expires_at,
            start - timedelta(minutes=self.restaurant.average_prep_time),
        )

    def test_solo_preorder_blocks_the_last_bucket_seat(self):
        self._set_capacity(1)
        slot = self._first_slot()
        start = _parse(slot['start'])
        Order.objects.create(
            customer=self.friend,
            restaurant=self.restaurant,
            status='SCHEDULED',
            is_paid=True,
            scheduled_for=start,
            items_subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
        )

        with self.assertRaisesMessage(PreorderSlotUnavailable, 'fully booked'):
            self._create_pool(self.initiator, start)

    def test_api_create_returns_the_booked_time(self):
        slot = self._first_slot()
        start = _parse(slot['start'])
        self.client.force_authenticate(self.initiator)

        response = self.client.post(
            '/api/pools/',
            {
                'restaurant': str(self.restaurant.id),
                'delivery_address': ADDRESS,
                'delivery_location': 'POINT(39.2083 -6.7924)',
                'scheduled_for': slot['start'],
            },
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(_parse(response.json()['scheduled_for']), start)
        self.assertEqual(response.json()['pool_type'], 'PREORDER')
        self.assertEqual(
            OrderPool.objects.get(id=response.json()['id']).scheduled_for,
            start,
        )
