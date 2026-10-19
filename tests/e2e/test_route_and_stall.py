"""
Route waypoints and batch stalling.

Two defects checked here:
  1. A batch emitted one pickup waypoint per *order*, so the optimiser routed
     through the same restaurant repeatedly and the driver was paid for
     phantom stops.
  2. A section that never goes READY parks its batch in PARTIAL_BATCH with no
     automatic recovery — it must at least alert an admin.
"""

from decimal import Decimal
from unittest.mock import patch

from apps.orders.models import BatchSectionStatus, DeliveryBatch, Order
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone


def _order(restaurant, order_id):
    return Order(
        id=order_id,
        restaurant=restaurant,
        restaurant_id=restaurant.id,
        delivery_location=Point(39.21, -6.79),
        delivery_address='Somewhere',
    )


class RouteWaypointTests(TestCase):
    """get_route_for_batch must emit one pickup per distinct restaurant."""

    def setUp(self):
        owner = User.objects.create_user(
            username='r_owner', phone='+255700000041',
            password='password123', role='vendor',
        )
        self.restaurant_a = Restaurant.objects.create(
            owner=owner, name='Cafe A', location='A',
            geo_location=Point(39.2083, -6.7924),
        )
        self.restaurant_b = Restaurant.objects.create(
            owner=owner, name='Cafe B', location='B',
            geo_location=Point(39.2090, -6.7930),
        )

    def _captured_coordinates(self, orders):
        from domain.services.mapbox_routing_service import MapboxRoutingService

        captured = {}

        def fake_optimize(coordinates, profile='walking'):
            captured['coords'] = coordinates
            return None  # force the fallback path; we only want the inputs

        def fake_directions(*args, **kwargs):
            # Never reach the network, whatever the token settings say.
            return None

        with patch.object(MapboxRoutingService, 'get_optimized_route', fake_optimize), \
             patch.object(MapboxRoutingService, 'get_directions_route', fake_directions), \
             patch.object(MapboxRoutingService, '_emit_event', lambda *a, **k: None):
            MapboxRoutingService.get_route_for_batch(orders)

        return captured.get('coords', [])

    def test_single_restaurant_emits_one_pickup(self):
        orders = [_order(self.restaurant_a, i) for i in (1, 2, 3)]
        coords = self._captured_coordinates(orders)

        pickups = [c for c in coords if c['type'] == 'pickup']
        deliveries = [c for c in coords if c['type'] == 'delivery']

        self.assertEqual(len(pickups), 1)
        self.assertEqual(len(deliveries), 3)
        # Four real stops, not five.
        self.assertEqual(len(coords), 4)

    def test_two_restaurants_emit_one_pickup_each(self):
        orders = [
            _order(self.restaurant_a, 1),
            _order(self.restaurant_a, 2),
            _order(self.restaurant_b, 3),
        ]
        coords = self._captured_coordinates(orders)

        pickups = [c for c in coords if c['type'] == 'pickup']
        self.assertEqual(len(pickups), 2)
        self.assertEqual(
            {p['restaurant_id'] for p in pickups},
            {str(self.restaurant_a.id), str(self.restaurant_b.id)},
        )
        # 2 pickups + 3 deliveries.
        self.assertEqual(len(coords), 5)

    def test_pickups_precede_deliveries(self):
        orders = [
            _order(self.restaurant_b, 1),
            _order(self.restaurant_a, 2),
        ]
        coords = self._captured_coordinates(orders)

        types = [c['type'] for c in coords]
        self.assertEqual(types, sorted(types, key=lambda t: 0 if t == 'pickup' else 1))

    def test_orders_without_location_are_skipped_not_fatal(self):
        class NoDelivery:
            restaurant = self.restaurant_a
            restaurant_id = self.restaurant_a.id
            delivery_location = None
            delivery_address = None
            id = 'x'

        good = _order(self.restaurant_b, 1)
        coords = self._captured_coordinates([NoDelivery(), good])

        self.assertEqual(len(coords), 2)
        self.assertEqual([c['type'] for c in coords], ['pickup', 'delivery'])


class BatchStallTests(TestCase):
    """A batch held by a stuck section must alert admins."""

    def setUp(self):
        owner = User.objects.create_user(
            username='stall_owner', phone='+255700000042',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner, name='Slow Kitchen', location='A',
            geo_location=Point(39.2083, -6.7924),
        )
        self.section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        Food.objects.create(
            section=self.section, name='Stew', price=Decimal('10000.00'),
        )
        self.customer = User.objects.create_user(
            username='stall_cust', phone='+255700000043',
            password='password123', role='customer',
        )
        self.batch = DeliveryBatch.objects.create(
            restaurant=self.restaurant, status='FINALIZING',
        )
        self.order = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='PREPARING',
            items_subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
            batch=self.batch,
        )
        self.bss = BatchSectionStatus.objects.create(
            batch=self.batch, section=self.section, status='PREPARING',
        )

    def test_stuck_section_escalates_and_alerts_admin(self):
        from domain.services.batch_pipeline import BatchPipeline

        # Backdate so the section trips the timeout.
        BatchSectionStatus.objects.filter(id=self.bss.id).update(
            updated_at=timezone.now() - timezone.timedelta(minutes=45),
        )

        with patch(
            'infrastructure.notifications.service.NotificationService.notify_admins'
        ) as notify:
            BatchPipeline.check_section_timeouts()

        self.batch.refresh_from_db()
        self.bss.refresh_from_db()
        self.assertEqual(self.batch.status, 'PARTIAL_BATCH')

        # A human has to know, because nothing recovers this automatically.
        notify.assert_called_once()
        data = notify.call_args.kwargs.get('data') or {}
        self.assertEqual(data.get('type'), 'batch_stalled')
        self.assertTrue(data.get('sms', False) or notify.call_args.kwargs.get('sms'))

    def test_stuck_section_alerts_admin_only_once(self):
        """Repeated poll cycles must not re-send the stalled alert (push loop)."""
        from domain.services.batch_pipeline import BatchPipeline

        BatchSectionStatus.objects.filter(id=self.bss.id).update(
            updated_at=timezone.now() - timezone.timedelta(minutes=45),
        )

        with patch(
            'infrastructure.notifications.service.NotificationService.notify_admins'
        ) as notify:
            BatchPipeline.check_section_timeouts()
            BatchPipeline.check_section_timeouts()
            BatchPipeline.check_section_timeouts()

        notify.assert_called_once()

    def test_partial_batch_never_reaches_pending_driver_on_its_own(self):
        """
        Documents the real limitation: PARTIAL_BATCH is a holding state.
        check_all_sections_ready still demands every section, so a stalled
        section keeps the run parked until a human intervenes.
        """
        from domain.services.batch_pipeline import BatchPipeline

        BatchSectionStatus.objects.filter(id=self.bss.id).update(
            updated_at=timezone.now() - timezone.timedelta(minutes=45),
        )
        with patch(
            'infrastructure.notifications.service.NotificationService.notify_admins'
        ):
            BatchPipeline.check_section_timeouts()
            BatchPipeline.check_all_sections_ready()

        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 'PARTIAL_BATCH')