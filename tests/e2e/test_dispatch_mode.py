"""
Dispatch authority policy.

Covers Restaurant.dispatch_mode: who may allocate a driver for a batch, which
audience the queue shows, and that changing the mode re-notifies whoever is
now responsible.
"""

from decimal import Decimal
from unittest.mock import patch

from apps.delivery.models import DeliveryAssignment, Driver
from apps.orders.models import DeliveryBatch, Order
from apps.products.models import Food
from apps.restaurants.models import (
    Restaurant,
    RestaurantSection,
    SectionStaff,
)
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from rest_framework.test import APIClient
from django.utils import timezone

POINT = Point(39.2083, -6.7924)


class DispatchModePolicyTests(TestCase):
    def setUp(self):
        # force_authenticate() lives on DRF's client, not Django's.
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='admin1', phone='+255700000010',
            password='password123', role='admin',
        )
        self.admin_mode_owner = User.objects.create_user(
            username='ownerA', phone='+255700000011',
            password='password123', role='vendor',
        )
        self.restaurant_mode_owner = User.objects.create_user(
            username='ownerR', phone='+255700000012',
            password='password123', role='vendor',
        )
        self.driver_user = User.objects.create_user(
            username='driver1', phone='+255700000013',
            password='password123', role='driver',
        )
        self.driver = Driver.objects.create(
            user=self.driver_user,
            status='AVAILABLE',
            driver_type='platform',
            location=POINT,
        )

        self.admin_mode_restaurant = Restaurant.objects.create(
            owner=self.admin_mode_owner,
            name='Admin Mode Cafe',
            location='Main Hall',
            geo_location=POINT,
            dispatch_mode=Restaurant.DISPATCH_ADMIN,
        )
        self.restaurant_mode_restaurant = Restaurant.objects.create(
            owner=self.restaurant_mode_owner,
            name='Self Dispatch Cafe',
            location='Library',
            geo_location=POINT,
            dispatch_mode=Restaurant.DISPATCH_RESTRANT,
        )

        self.admin_mode_batch = self._make_ready_batch(
            self.admin_mode_restaurant, 'AdmOrd', '800000001',
        )
        self.restaurant_mode_batch = self._make_ready_batch(
            self.restaurant_mode_restaurant, 'ResOrd', '800000002',
        )

    def _make_ready_batch(self, restaurant, name, phone):
        section = RestaurantSection.objects.create(
            restaurant=restaurant, name=f'{name} section',
        )
        Food.objects.create(
            section=section, name=f'{name} item', price=Decimal('5000.00'),
        )
        order = Order.objects.create(
            customer=User.objects.create_user(
                username=f'cust{name}', phone=f'+255{phone}',
                password='password123', role='customer',
            ),
            restaurant=restaurant,
            status='READY',
            items_subtotal=Decimal('5000.00'),
            total_amount=Decimal('5500.00'),
        )
        batch = DeliveryBatch.objects.create(
            restaurant=restaurant, status='PENDING_DRIVER',
        )
        order.batch = batch
        order.save(update_fields=['batch'])
        return batch

    # ── Resolution ────────────────────────────────────────────────────────────

    def test_new_restaurants_default_to_admin_dispatch(self):
        restaurant = Restaurant.objects.create(
            owner=self.restaurant_mode_owner, name='Defaulted Cafe',
            location='x',
        )
        self.assertEqual(restaurant.dispatch_mode, Restaurant.DISPATCH_ADMIN)

    def test_batch_resolves_to_its_restaurant_mode(self):
        from domain.services.dispatch_routing import resolve_batch_dispatch_mode

        self.assertEqual(
            resolve_batch_dispatch_mode(self.admin_mode_batch),
            Restaurant.DISPATCH_ADMIN,
        )
        self.assertEqual(
            resolve_batch_dispatch_mode(self.restaurant_mode_batch),
            Restaurant.DISPATCH_RESTRANT,
        )

    def test_mixed_batch_resolves_to_admin(self):
        """Admin wins when a batch spans both modes — the safest rule."""
        from domain.services.dispatch_routing import resolve_batch_dispatch_mode

        batch = DeliveryBatch.objects.create(status='PENDING_DRIVER')
        batch.orders.add(*list(Order.objects.filter(
            restaurant__in=[self.admin_mode_restaurant, self.restaurant_mode_restaurant],
        )))

        self.assertEqual(
            resolve_batch_dispatch_mode(batch), Restaurant.DISPATCH_ADMIN,
        )

    def test_batch_with_no_orders_resolves_to_admin(self):
        from domain.services.dispatch_routing import resolve_batch_dispatch_mode

        batch = DeliveryBatch.objects.create(status='PENDING_DRIVER')
        self.assertEqual(
            resolve_batch_dispatch_mode(batch), Restaurant.DISPATCH_ADMIN,
        )

    # ── Allocation authority ──────────────────────────────────────────────────

    def test_vendor_cannot_allocate_in_admin_mode(self):
        from domain.services.permissions import assert_can_allocate_drivers

        with self.assertRaises(PermissionError):
            assert_can_allocate_drivers(
                self.admin_mode_owner, Restaurant.DISPATCH_ADMIN,
            )

    def test_vendor_can_allocate_in_restaurant_mode(self):
        from domain.services.permissions import assert_can_allocate_drivers

        source = assert_can_allocate_drivers(
            self.restaurant_mode_owner, Restaurant.DISPATCH_RESTRANT,
        )
        self.assertEqual(source, DeliveryAssignment.SOURCE_VENDOR)

    def test_admin_can_allocate_in_either_mode(self):
        """Escalation — the platform must never be blocked by a quiet restaurant."""
        from domain.services.permissions import assert_can_allocate_drivers

        for mode in (Restaurant.DISPATCH_ADMIN, Restaurant.DISPATCH_RESTRANT):
            with self.subTest(mode=mode):
                source = assert_can_allocate_drivers(self.admin, mode)
                self.assertEqual(source, DeliveryAssignment.SOURCE_ADMIN)

    def test_driver_cannot_allocate_in_either_mode(self):
        from domain.services.permissions import assert_can_allocate_drivers

        for mode in (Restaurant.DISPATCH_ADMIN, Restaurant.DISPATCH_RESTRANT):
            with self.subTest(mode=mode):
                with self.assertRaises(PermissionError):
                    assert_can_allocate_drivers(self.driver_user, mode)

    def test_stall_staff_can_allocate_in_restaurant_mode(self):
        """
        Stall staff are stored with role='customer' and only derive
        'vendor_staff' from having a SectionStaff row — the allocation policy
        must key off the derived role, not the raw column.
        """
        from domain.services.permissions import assert_can_allocate_drivers

        section = RestaurantSection.objects.create(
            restaurant=self.restaurant_mode_restaurant, name='Kiosk',
        )
        stall_user = User.objects.create_user(
            username='stall1', phone='+255700000014',
            password='password123', role='customer',
        )
        SectionStaff.objects.create(user=stall_user, section=section)

        self.assertEqual(stall_user.serialized_role, 'vendor_staff')

        source = assert_can_allocate_drivers(
            stall_user, Restaurant.DISPATCH_RESTRANT,
        )
        self.assertEqual(source, DeliveryAssignment.SOURCE_VENDOR)

    # ── Audit trail ───────────────────────────────────────────────────────────

    def test_assignment_source_is_derived_from_actor(self):
        from domain.services.orders.order_assignment import AssignDriverService

        order = Order.objects.filter(
            restaurant=self.restaurant_mode_restaurant,
        ).first()

        AssignDriverService.execute(
            str(order.id), str(self.driver.id), self.restaurant_mode_owner,
        )

        assignment = DeliveryAssignment.objects.get(order=order)
        self.assertEqual(
            assignment.assignment_source, DeliveryAssignment.SOURCE_VENDOR,
        )
        self.assertEqual(
            assignment.assigned_by_id, self.restaurant_mode_owner.id,
        )

    # ── Endpoints ─────────────────────────────────────────────────────────────

    def test_admin_queue_lists_both_modes(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get('/api/admin/pending-batches/')

        self.assertEqual(response.status_code, 200)
        modes = {b['dispatch_mode'] for b in response.json()['batches']}
        self.assertEqual(modes, {'admin', 'restaurant'})

    def test_assign_driver_allows_vendor_only_in_restaurant_mode(self):
        url = f'/api/batches/{self.restaurant_mode_batch.id}/assign-driver/'
        self.client.force_authenticate(self.restaurant_mode_owner)
        response = self.client.post(
            url, {'driver_id': str(self.driver.id)}, format='json',
        )
        self.assertEqual(response.status_code, 200)

        admin_url = f'/api/batches/{self.admin_mode_batch.id}/assign-driver/'
        self.client.force_authenticate(self.admin_mode_owner)
        response = self.client.post(
            admin_url, {'driver_id': str(self.driver.id)}, format='json',
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_may_escalate_a_restaurant_mode_batch(self):
        url = f'/api/batches/{self.restaurant_mode_batch.id}/assign-driver/'
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            url, {'driver_id': str(self.driver.id)}, format='json',
        )
        self.assertEqual(response.status_code, 200)

        order = Order.objects.filter(
            restaurant=self.restaurant_mode_restaurant,
        ).first()
        self.assertEqual(
            DeliveryAssignment.objects.get(order=order).assignment_source,
            DeliveryAssignment.SOURCE_ADMIN,
        )

    # ── Edit path ─────────────────────────────────────────────────────────────

    def test_invalid_dispatch_mode_is_rejected(self):
        from domain.services.admin_services import ManageRestaurantService

        with self.assertRaises(ValueError):
            ManageRestaurantService.update_restaurant(
                str(self.admin_mode_restaurant.id), dispatch_mode='nonsense',
            )

    def test_admin_can_change_dispatch_mode(self):
        from domain.services.admin_services import ManageRestaurantService

        updated = ManageRestaurantService.update_restaurant(
            str(self.admin_mode_restaurant.id),
            dispatch_mode=Restaurant.DISPATCH_RESTRANT,
        )
        self.assertEqual(updated.dispatch_mode, Restaurant.DISPATCH_RESTRANT)

    def test_changing_mode_resets_throttle_and_renotifies(self):
        """
        Handing dispatch to the restaurant must also hand over batches that are
        already waiting, or they stall with nobody notified — batched orders are
        exempt from the unassigned-expiry task.
        """
        from domain.services.admin_services import ManageRestaurantService

        batch = self.admin_mode_batch
        batch.offer_sent_at = timezone.now()
        batch.save(update_fields=['offer_sent_at'])

        with patch('domain.services.admin_services._renotify_batches') as renotify:
            with self.captureOnCommitCallbacks(execute=True):
                ManageRestaurantService.update_restaurant(
                    str(self.admin_mode_restaurant.id),
                    dispatch_mode=Restaurant.DISPATCH_RESTRANT,
                )

        batch.refresh_from_db()
        self.assertIsNone(batch.offer_sent_at)
        renotify.assert_called_once_with([str(batch.id)])