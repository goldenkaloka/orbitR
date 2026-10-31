"""
Scheduled buckets: member orders wait in SCHEDULED and reach the kitchen at
release time as one group, not one order at a time.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from apps.orders.models import Order, OrderPool, PoolMember
from apps.orders.tasks import release_scheduled_orders
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from domain.events.order_handlers import _notify_order_event
from domain.events.pool_handlers import on_pool_ready
from domain.events.types import OrderConfirmed, PoolReady
from domain.services.orders.order_pooling import PoolOrderService
from domain.services.orders.scheduled_orders import slot_window_for

POINT = Point(39.2083, -6.7924)
ADDRESS = '12 Slots Road'


class ScheduledPoolReleaseTests(TestCase):
    def setUp(self):
        self.initiator = User.objects.create_user(
            username='rel_host', phone='+255700001050',
            password='password123', role='customer',
        )
        self.friend = User.objects.create_user(
            username='rel_friend', phone='+255700001051',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='owner_rel', phone='+255700001052',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Release Diner',
            preorders_enabled=True,
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=25,
            preorder_slot_capacity=2,
        )
        section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        self.food = Food.objects.create(
            section=section, name='Release Plate', price=Decimal('10000.00'),
        )
        self.scheduled_for = (
            timezone.now() + timedelta(hours=3)
        ).replace(second=0, microsecond=0)

        for target in (
            'domain.services.orders.order_pooling.broadcast_pool_status',
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _pool(self, scheduled_for):
        pool = OrderPool.objects.create(
            initiator=self.initiator,
            restaurant=self.restaurant,
            invite_code=uuid.uuid4().hex[:6].upper(),
            delivery_address=ADDRESS,
            delivery_location=POINT,
            expires_at=timezone.now() + timedelta(hours=2),
            scheduled_for=scheduled_for,
            status='LOCKED',
        )
        for user in (self.initiator, self.friend):
            PoolMember.objects.create(
                pool=pool,
                user=user,
                status='PAID',
                items=[{
                    'food_id': str(self.food.id),
                    'quantity': 2,
                    'price': '10000.00',
                }],
                items_subtotal=Decimal('20000.00'),
                delivery_fee_share=Decimal('500.00'),
                service_fee_share=Decimal('200.00'),
            )
        return pool

    def _finalize(self, pool):
        with (
            patch('domain.services.orders.order_pooling.EventBus.publish'),
            patch('domain.services.orders.order_pooling._notify_pool_prep_sms') as notify,
            self.captureOnCommitCallbacks(execute=True),
        ):
            PoolOrderService.finalize_pool(str(pool.id))
        return notify

    def test_scheduled_pool_members_wait_in_scheduled(self):
        pool = self._pool(self.scheduled_for)

        notify = self._finalize(pool)

        orders = list(Order.objects.filter(pool=pool))
        self.assertEqual(len(orders), 2)
        for order in orders:
            self.assertEqual(order.status, 'SCHEDULED')
            self.assertEqual(order.scheduled_for, self.scheduled_for)
            self.assertEqual(
                order.release_at,
                self.scheduled_for - timedelta(minutes=25),
            )
            self.assertTrue(order.is_paid)
            self.assertIsNone(order.confirmed_at)
        self.assertEqual(pool.pool_type, 'PREORDER')
        notify.assert_not_called()

    def test_immediate_pool_still_finalizes_confirmed(self):
        pool = self._pool(None)

        notify = self._finalize(pool)

        orders = list(Order.objects.filter(pool=pool))
        self.assertEqual(len(orders), 2)
        for order in orders:
            self.assertEqual(order.status, 'CONFIRMED')
            self.assertIsNone(order.scheduled_for)
            self.assertIsNone(order.release_at)
            self.assertIsNotNone(order.confirmed_at)
        self.assertEqual(pool.pool_type, 'IMMEDIATE')
        notify.assert_called_once_with(str(pool.id))

    def test_release_moves_the_whole_pool_to_the_kitchen_once(self):
        pool = self._pool(self.scheduled_for)
        self._finalize(pool)
        Order.objects.filter(pool=pool).update(
            release_at=timezone.now() - timedelta(minutes=1),
        )

        with patch(
            'domain.services.orders.order_pooling._notify_pool_prep_sms',
        ) as notify:
            release_scheduled_orders()

        self.assertEqual(
            set(Order.objects.filter(pool=pool).values_list('status', flat=True)),
            {'CONFIRMED'},
        )
        pool.refresh_from_db()
        self.assertEqual(pool.status, 'PAID')
        notify.assert_called_once_with(str(pool.id))

    def test_pool_releases_as_one_unit_when_member_times_differ(self):
        pool = self._pool(self.scheduled_for)
        self._finalize(pool)
        orders = list(Order.objects.filter(pool=pool).order_by('created_at'))
        Order.objects.filter(id=orders[0].id).update(
            release_at=timezone.now() - timedelta(minutes=1),
        )
        Order.objects.filter(id=orders[1].id).update(
            release_at=timezone.now() + timedelta(hours=1),
        )

        with patch(
            'domain.services.orders.order_pooling._notify_pool_prep_sms',
        ) as notify:
            release_scheduled_orders()

        # A bucket is one delivery: the earliest member drags the rest of the
        # bucket into the kitchen, and the kitchen gets one prep SMS.
        notify.assert_called_once_with(str(pool.id))
        self.assertEqual(Order.objects.get(id=orders[0].id).status, 'CONFIRMED')
        self.assertEqual(Order.objects.get(id=orders[1].id).status, 'CONFIRMED')

    def test_released_pool_order_does_not_sms_the_kitchen_per_member(self):
        pool = self._pool(self.scheduled_for)
        self._finalize(pool)
        order = Order.objects.filter(pool=pool).first()

        with (
            patch(
                'infrastructure.notifications.service.NotificationService.send_push',
            ) as push,
            patch('apps.events.utils.broadcast_order_status'),
            patch('apps.events.webhooks.fire_order_webhook'),
        ):
            _notify_order_event(OrderConfirmed(
                aggregate_id=order.id, payload={},
            ))

        # Only the customer is told; the kitchen gets one pool-level message.
        self.assertEqual(push.call_count, 1)

    def test_preorder_bucket_dispatches_as_one_slot_tagged_batch(self):
        booked_for = timezone.now() + timedelta(minutes=10)
        pool = self._pool(booked_for)
        self._finalize(pool)
        with patch(
            'domain.services.orders.order_pooling._notify_pool_prep_sms',
        ):
            release_scheduled_orders()
        pool.refresh_from_db()
        PoolOrderService.mark_pool_ready(str(pool.id), self.restaurant.owner)

        with patch(
            'domain.services.mapbox_routing_service.MapboxRoutingService.get_route_for_batch',
            return_value=None,
        ):
            on_pool_ready(PoolReady(aggregate_id=pool.id, payload={}))

        pool.refresh_from_db()
        self.assertEqual(pool.status, 'DISPATCHING')
        self.assertIsNotNone(pool.batch_id)
        self.assertEqual(
            pool.batch.preorder_slot_start,
            slot_window_for(booked_for)[0],
        )
        self.assertEqual(
            set(Order.objects.filter(pool=pool).values_list('batch_id', flat=True)),
            {pool.batch_id},
        )

    def test_preorder_bucket_cannot_start_prep_before_release(self):
        pool = self._pool(self.scheduled_for)
        self._finalize(pool)

        with self.assertRaisesMessage(ValueError, 'later eating time'):
            PoolOrderService.mark_pool_ready(
                str(pool.id), self.restaurant.owner,
            )
