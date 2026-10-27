"""
The bucket's delivery fee must exist before the host locks it.

A member opening the bucket saw "Delivery fee TZS 0" until the lock action
priced it. The fee is now computed when the bucket is created and re-split
on every membership or item change.
"""

from decimal import Decimal
from unittest.mock import patch

from apps.orders.models import OrderPool, PoolMember
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from domain.services.orders.order_pooling import PoolOrderService

POINT = Point(39.2083, -6.7924)
ADDRESS = '12 Bucket Road'


class PoolFeePreviewTests(TestCase):
    def setUp(self):
        self.host = User.objects.create_user(
            username='fee_host', phone='+255700002040',
            password='password123', role='customer',
        )
        self.friend = User.objects.create_user(
            username='fee_friend', phone='+255700002041',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='fee_owner', phone='+255700002042',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Fee Diner',
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=20,
        )
        section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        self.food = Food.objects.create(
            section=section, name='Fee Plate', price=Decimal('10000.00'),
        )

        for target in (
            'domain.services.orders.order_pooling.notify_nearby_pool_created',
            'domain.services.orders.order_pooling.broadcast_pool_status',
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _create_pool(self):
        return PoolOrderService.create_pool(
            initiator=self.host,
            restaurant_id=self.restaurant.id,
            delivery_address=ADDRESS,
            delivery_location=POINT,
        )

    def _shares(self, pool):
        return {
            str(member.id): member.delivery_fee_share
            for member in pool.members.all()
        }

    def test_fee_is_priced_before_lock(self):
        pool = self._create_pool()
        pool.refresh_from_db()

        self.assertGreater(pool.total_delivery_fee, Decimal('0'))

        member = pool.members.get(user=self.host)
        self.assertEqual(member.delivery_fee_share, pool.total_delivery_fee)

    def test_fee_re_splits_when_a_member_joins(self):
        pool = self._create_pool()
        PoolOrderService.add_item(self.host, pool.id, self.food.id, 2)
        total_after_host = OrderPool.objects.get(id=pool.id).total_delivery_fee

        PoolOrderService.join_pool(self.friend, pool.invite_code)
        PoolOrderService.add_item(self.friend, pool.id, self.food.id, 1)

        pool.refresh_from_db()
        shares = self._shares(pool)

        self.assertEqual(len(shares), 2)
        self.assertEqual(sum(shares.values()), pool.total_delivery_fee)
        self.assertGreater(pool.total_delivery_fee, Decimal('0'))
        # The total is a property of the route, not of the squad size.
        self.assertEqual(pool.total_delivery_fee, total_after_host)

    def test_fee_collapses_back_when_a_member_leaves(self):
        pool = self._create_pool()
        PoolOrderService.join_pool(self.friend, pool.invite_code)
        PoolOrderService.leave_pool(self.friend, pool.id)

        pool.refresh_from_db()
        member = pool.members.get(user=self.host)

        self.assertEqual(pool.members.count(), 1)
        self.assertEqual(member.delivery_fee_share, pool.total_delivery_fee)
        self.assertGreater(pool.total_delivery_fee, Decimal('0'))

    def test_member_share_tracks_items(self):
        pool = self._create_pool()
        PoolOrderService.join_pool(self.friend, pool.invite_code)
        PoolOrderService.add_item(self.friend, pool.id, self.food.id, 3)

        pool.refresh_from_db()
        host = pool.members.get(user=self.host)
        friend = pool.members.get(user=self.friend)

        # Friend holds the whole subtotal, so they carry the delivery share.
        self.assertEqual(friend.delivery_fee_share, pool.total_delivery_fee)
        self.assertEqual(host.delivery_fee_share, Decimal('0'))
