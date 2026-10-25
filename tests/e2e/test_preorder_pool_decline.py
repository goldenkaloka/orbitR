"""
Restaurant-side decline and safe cancellation for pre-order buckets.

Cancelling a paid bucket used to refund the members and flip the pool status
while the finalized member orders stayed live — customers kept waiting on a
meal nobody was going to cook. These tests pin down both paths: the restaurant
declining the whole bucket, and the initiator/admin cancelling it.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from apps.events.models import EventLog
from apps.orders.models import Order, OrderPool, PoolMember
from apps.payments.models import Payment
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from domain.services.orders.order_pooling import PoolOrderService
from rest_framework.test import APIClient

POINT = Point(39.2083, -6.7924)
ADDRESS = '12 Decline Road'


class PoolDeclineTestMixin:
    def setUp(self):
        self.initiator = User.objects.create_user(
            username='bucket_host', phone='+255700001050',
            password='password123', role='customer',
        )
        self.friend = User.objects.create_user(
            username='bucket_friend', phone='+255700001051',
            password='password123', role='customer',
        )
        self.owner = User.objects.create_user(
            username='bucket_owner', phone='+255700001052',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=self.owner,
            name='Bucket Diner',
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=25,
            preorder_slot_capacity=4,
        )
        RestaurantSection.objects.create(restaurant=self.restaurant, name='Main')
        # Four hours out keeps the bucket clear of the lead-time window.
        self.scheduled_for = (timezone.now() + timedelta(hours=4)).replace(
            second=0, microsecond=0,
        )
        self.client = APIClient()

        for target in (
            'domain.services.orders.order_pooling.notify_nearby_pool_created',
            'apps.events.utils.broadcast_pool_status',
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make_paid_bucket(self):
        pool = OrderPool.objects.create(
            initiator=self.initiator,
            restaurant=self.restaurant,
            invite_code='DECLNE',
            delivery_address=ADDRESS,
            delivery_location=POINT,
            expires_at=self.scheduled_for - timedelta(minutes=25),
            scheduled_for=self.scheduled_for,
        )
        members = [
            PoolMember.objects.create(
                pool=pool, user=self.initiator, status='PAID',
                items_subtotal=Decimal('10000.00'),
            ),
            PoolMember.objects.create(
                pool=pool, user=self.friend, status='PAID',
                items_subtotal=Decimal('5000.00'),
            ),
        ]
        pool.status = 'PAID'
        pool.save(update_fields=['status'])

        orders, payments = [], []
        for member in members:
            amount = member.items_subtotal
            orders.append(Order.objects.create(
                customer=member.user,
                restaurant=self.restaurant,
                pool=pool,
                is_pool_order=True,
                is_paid=True,
                status='SCHEDULED',
                scheduled_for=self.scheduled_for,
                items_subtotal=amount,
                total_amount=amount,
            ))
            payments.append(Payment.objects.create(
                payable_type='pool_member',
                payable_id=str(member.id),
                amount=amount,
                status='COLLECTED',
            ))
        return pool, members, orders, payments


class PoolDeclineTests(PoolDeclineTestMixin, TestCase):
    def test_decline_refunds_every_member_and_cancels_their_orders(self):
        pool, members, orders, payments = self._make_paid_bucket()

        with self.captureOnCommitCallbacks(execute=True):
            PoolOrderService.decline_pool(str(pool.id), self.owner)

        pool.refresh_from_db()
        self.assertEqual(pool.status, 'CANCELLED')

        for payment in payments:
            payment.refresh_from_db()
            self.assertEqual(payment.status, 'REFUNDED')
            self.assertEqual(
                payment.transactions.filter(
                    transaction_type='platform_to_customer_refund',
                ).count(),
                1,
            )
            self.assertFalse(
                payment.transactions.filter(
                    transaction_type='platform_cancel_fee',
                ).exists()
            )

        for member in members:
            member.refresh_from_db()
            self.assertEqual(member.status, 'REFUNDED')

        for order in orders:
            order.refresh_from_db()
            self.assertEqual(order.status, 'CANCELLED')
            self.assertEqual(order.cancelled_by, 'VENDOR')
            self.assertEqual(order.cancellation_fault, 'VENDOR')
            self.assertEqual(order.cancellation_category, 'vendor_rejected')
            self.assertTrue(order.affects_restaurant_reputation)

        self.assertEqual(
            EventLog.objects.filter(event_type='OrderCancelled').count(),
            len(orders),
        )
        self.assertEqual(
            EventLog.objects.filter(event_type='PoolCancelled').count(), 1,
        )

    def test_decline_is_idempotent_when_the_pool_is_already_cancelled(self):
        pool, _, _, payments = self._make_paid_bucket()

        PoolOrderService.decline_pool(str(pool.id), self.owner)

        with self.assertRaisesMessage(ValueError, 'Cannot decline'):
            PoolOrderService.decline_pool(str(pool.id), self.owner)

        for payment in payments:
            payment.refresh_from_db()
            self.assertEqual(
                payment.transactions.filter(
                    transaction_type='platform_to_customer_refund',
                ).count(),
                1,
            )

    def test_customer_cannot_decline_the_bucket(self):
        pool, _, orders, payments = self._make_paid_bucket()

        with self.assertRaises(PermissionError):
            PoolOrderService.decline_pool(str(pool.id), self.initiator)

        pool.refresh_from_db()
        self.assertEqual(pool.status, 'PAID')
        for order in orders:
            order.refresh_from_db()
            self.assertEqual(order.status, 'SCHEDULED')
        for payment in payments:
            payment.refresh_from_db()
            self.assertEqual(payment.status, 'COLLECTED')

    def test_another_restaurants_owner_cannot_decline(self):
        intruder = User.objects.create_user(
            username='other_bucket_owner', phone='+255700001053',
            password='password123', role='vendor',
        )
        pool, _, _, _ = self._make_paid_bucket()

        with self.assertRaises(PermissionError):
            PoolOrderService.decline_pool(str(pool.id), intruder)

        pool.refresh_from_db()
        self.assertEqual(pool.status, 'PAID')

    def test_only_scheduled_buckets_can_be_declined(self):
        pool, _, _, _ = self._make_paid_bucket()
        pool.scheduled_for = None
        pool.save(update_fields=['scheduled_for', 'pool_type'])

        with self.assertRaisesMessage(ValueError, 'Only scheduled'):
            PoolOrderService.decline_pool(str(pool.id), self.owner)

        pool.refresh_from_db()
        self.assertEqual(pool.status, 'PAID')

    def test_api_endpoint_rejects_customers(self):
        pool, _, _, _ = self._make_paid_bucket()
        self.client.force_authenticate(self.initiator)

        response = self.client.post(f'/api/pools/{pool.id}/pool-decline/')

        self.assertEqual(response.status_code, 403)
        pool.refresh_from_db()
        self.assertEqual(pool.status, 'PAID')

    def test_api_endpoint_allows_the_restaurant(self):
        pool, _, _, _ = self._make_paid_bucket()
        self.client.force_authenticate(self.owner)

        response = self.client.post(
            f'/api/pools/{pool.id}/pool-decline/',
            {'reason': 'Kitchen is fully booked'},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'CANCELLED')
        self.assertEqual(
            Order.objects.get(pool=pool, customer=self.friend).cancellation_reason,
            'Kitchen is fully booked',
        )


class PoolCancellationTests(PoolDeclineTestMixin, TestCase):
    def test_cancelling_a_paid_bucket_cancels_its_parked_orders(self):
        pool, _, orders, payments = self._make_paid_bucket()

        with self.captureOnCommitCallbacks(execute=True):
            PoolOrderService.cancel_pool(str(pool.id), self.initiator)

        pool.refresh_from_db()
        self.assertEqual(pool.status, 'CANCELLED')

        for order in orders:
            order.refresh_from_db()
            self.assertEqual(order.status, 'CANCELLED')
            self.assertEqual(order.cancelled_by, 'CUSTOMER')
            self.assertEqual(order.cancellation_category, 'pool_cancelled')
            self.assertFalse(order.affects_restaurant_reputation)

        for payment in payments:
            payment.refresh_from_db()
            self.assertEqual(payment.status, 'REFUNDED')
            self.assertEqual(
                payment.transactions.filter(
                    transaction_type='platform_to_customer_refund',
                ).count(),
                1,
            )

        self.assertEqual(
            EventLog.objects.filter(event_type='OrderCancelled').count(),
            len(orders),
        )

    def test_restaurant_cancellation_is_recorded_as_a_vendor_fault(self):
        pool, _, orders, _ = self._make_paid_bucket()

        with self.captureOnCommitCallbacks(execute=True):
            PoolOrderService.cancel_pool(str(pool.id), self.owner)

        for order in orders:
            order.refresh_from_db()
            self.assertEqual(order.status, 'CANCELLED')
            self.assertEqual(order.cancelled_by, 'VENDOR')
            self.assertEqual(order.cancellation_fault, 'VENDOR')
            self.assertEqual(order.cancellation_category, 'vendor_rejected')
            self.assertTrue(order.affects_restaurant_reputation)

    def test_already_delivered_orders_are_left_alone(self):
        pool, _, orders, _ = self._make_paid_bucket()
        delivered = orders[0]
        delivered.status = 'DELIVERED'
        delivered.save(update_fields=['status'])

        PoolOrderService.cancel_pool(str(pool.id), self.initiator)

        delivered.refresh_from_db()
        self.assertEqual(delivered.status, 'DELIVERED')
        other = orders[1]
        other.refresh_from_db()
        self.assertEqual(other.status, 'CANCELLED')
