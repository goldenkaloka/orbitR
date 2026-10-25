"""
Slot availability for the pre-order picker.

The API must only offer times the customer can actually book: inside the lead
window, inside the horizon, and with kitchen capacity left in that slot.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from apps.orders.models import Order
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

POINT = Point(39.2083, -6.7924)


class PreorderSlotsApiTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='cust_slots', phone='+255700001030',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='owner_slots', phone='+255700001031',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Slots Diner',
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=25,
            preorder_slot_capacity=2,
        )
        section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        Food.objects.create(
            section=section, name='Slot Plate', price=Decimal('10000.00'),
        )
        self.client = APIClient()

    def _url(self, days=2):
        return f'/api/restaurants/{self.restaurant.id}/preorder-slots/?days={days}'

    def _payload(self, days=2):
        response = self.client.get(self._url(days))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _first_slot(self, payload):
        for day in payload['days']:
            for slot in day['slots']:
                if slot['available']:
                    return slot
        self.fail('API returned no bookable slots')

    def _book(self, slot):
        return Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='SCHEDULED',
            is_paid=True,
            items_subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
            scheduled_for=datetime.fromisoformat(slot['start']),
        )

    def test_slots_are_public_and_inside_the_booking_window(self):
        payload = self._payload()

        self.assertTrue(payload['accepting_preorders'])
        self.assertTrue(payload['days'])
        self.assertEqual(payload['slot_minutes'], 15)

        earliest = timezone.now() + timedelta(
            minutes=payload['lead_time_minutes'],
        ) - timedelta(seconds=2)
        latest = timezone.now() + timedelta(days=payload['max_horizon_days'])
        for day in payload['days']:
            for slot in day['slots']:
                start = datetime.fromisoformat(slot['start'])
                self.assertGreater(start, earliest)
                self.assertLessEqual(start, latest)
                self.assertIn('label', slot)

    def test_booking_consumes_one_seat_in_the_slot(self):
        payload = self._payload()
        slot = self._first_slot(payload)
        self.assertEqual(slot['remaining'], slot['capacity'])

        order = self._book(slot)
        self.assertEqual(
            order.scheduled_for,
            datetime.fromisoformat(slot['start']),
        )

        after = self._payload()
        updated = next(
            s
            for day in after['days']
            for s in day['slots']
            if s['start'] == slot['start']
        )
        self.assertEqual(updated['remaining'], slot['remaining'] - 1)
        self.assertTrue(updated['available'])

    def test_full_slot_is_reported_unavailable(self):
        self.restaurant.preorder_slot_capacity = 1
        self.restaurant.save(update_fields=['preorder_slot_capacity'])

        payload = self._payload()
        slot = self._first_slot(payload)
        self._book(slot)

        after = self._payload()
        updated = next(
            s
            for day in after['days']
            for s in day['slots']
            if s['start'] == slot['start']
        )
        self.assertEqual(updated['remaining'], 0)
        self.assertFalse(updated['available'])

    def test_restaurant_can_disable_preorders(self):
        self.restaurant.preorder_slot_capacity = 0
        self.restaurant.save(update_fields=['preorder_slot_capacity'])

        payload = self._payload()

        self.assertFalse(payload['accepting_preorders'])
        self.assertEqual(payload['days'], [])

    def test_days_are_capped_by_the_booking_horizon(self):
        payload = self._payload(days=30)

        latest = timezone.now() + timedelta(
            days=payload['max_horizon_days'],
        )
        for day in payload['days']:
            for slot in day['slots']:
                self.assertLessEqual(datetime.fromisoformat(slot['start']), latest)
