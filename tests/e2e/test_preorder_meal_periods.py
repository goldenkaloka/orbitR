"""
Meal periods: breakfast, lunch and dinner shape when pre-orders are allowed.

A restaurant without configured windows serves the platform defaults; its own
rows replace them, each meal can cap its own slots, and times between services
are not bookable at all.
"""

from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from apps.orders.models import Order
from apps.restaurants.models import MealPeriod, Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase, override_settings
from domain.services.orders.scheduled_orders import (
    PreorderSlotUnavailable,
    accepts_preorders,
    ensure_preorder_slot_capacity,
    meal_window_for,
    meal_windows_for,
    release_at_for,
    upcoming_slot_days,
)
from rest_framework.test import APIClient

POINT = Point(39.2083, -6.7924)
TZ = ZoneInfo('Africa/Dar_es_Salaam')


@override_settings(
    PREORDER_TIMEZONE='Africa/Dar_es_Salaam',
    PREORDER_SERVICE_START_HOUR=8,
    PREORDER_SERVICE_END_HOUR=22,
    PREORDER_SLOT_MINUTES=15,
    PREORDER_MIN_LEAD_MINUTES=45,
    PREORDER_MAX_HORIZON_DAYS=7,
)
class MealPeriodTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='meal_customer', phone='+255700001050',
            password='password123', role='customer',
        )
        self.owner = User.objects.create_user(
            username='meal_owner', phone='+255700001051',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=self.owner,
            name='Meal Diner',
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=20,
            preorder_slot_capacity=3,
        )
        RestaurantSection.objects.create(restaurant=self.restaurant, name='Main')
        # 06:00 local: the whole first service day is still ahead.
        self.now = datetime(2026, 10, 25, 6, 0, tzinfo=TZ)

    def _local(self, hour, minute=0, day=25):
        return datetime(2026, 10, day, hour, minute, tzinfo=TZ)

    def _add_period(self, kind, start, end, capacity=None, active=True, cutoff=None):
        return MealPeriod.objects.create(
            restaurant=self.restaurant,
            kind=kind,
            start_time=start,
            end_time=end,
            preorder_cutoff_time=cutoff,
            slot_capacity=capacity,
            is_active=active,
        )

    def _book(self, scheduled_for):
        ensure_preorder_slot_capacity(self.restaurant, scheduled_for)
        return Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='SCHEDULED',
            is_paid=True,
            items_subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
            scheduled_for=scheduled_for,
            release_at=release_at_for(self.restaurant, scheduled_for),
        )

    def _slots(self):
        response = APIClient().get(
            f'/api/restaurants/{self.restaurant.id}/preorder-slots/?days=2'
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_default_windows_are_breakfast_lunch_and_dinner(self):
        windows = meal_windows_for(self.restaurant)

        self.assertEqual(
            [(window.kind, window.start, window.end) for window in windows],
            [
                ('BREAKFAST', time(8, 0), time(10, 30)),
                ('LUNCH', time(12, 0), time(15, 30)),
                ('DINNER', time(18, 0), time(22, 0)),
            ],
        )

    def test_days_group_slots_by_meal_and_keep_the_flat_list(self):
        day = upcoming_slot_days(self.restaurant, days=1, now=self.now)[0]

        self.assertEqual(
            [meal['type'] for meal in day['meals']],
            ['BREAKFAST', 'LUNCH', 'DINNER'],
        )
        self.assertEqual(
            [meal['label'] for meal in day['meals']],
            ['Breakfast', 'Lunch', 'Dinner'],
        )
        self.assertEqual(day['meals'][0]['slots'][0]['label'], '08:00')
        self.assertEqual(day['meals'][1]['slots'][0]['label'], '12:00')
        self.assertEqual(day['meals'][2]['slots'][0]['label'], '18:00')
        self.assertEqual(
            len(day['slots']),
            sum(len(meal['slots']) for meal in day['meals']),
        )
        self.assertTrue(
            all(slot['meal_period'] for slot in day['slots'])
        )

    def test_between_meals_is_not_bookable(self):
        with self.assertRaisesMessage(
            PreorderSlotUnavailable, 'only accepted during',
        ):
            ensure_preorder_slot_capacity(self.restaurant, self._local(11, 0))

    def test_meal_boundary_is_half_open(self):
        self.assertIsNotNone(
            meal_window_for(self.restaurant, self._local(10, 29)),
        )
        self.assertIsNone(meal_window_for(self.restaurant, self._local(10, 30)))

    def test_configured_windows_replace_the_defaults(self):
        self._add_period(MealPeriod.DINNER, time(19, 0), time(21, 0))

        windows = meal_windows_for(self.restaurant)
        self.assertEqual([window.kind for window in windows], ['DINNER'])

        with self.assertRaisesMessage(
            PreorderSlotUnavailable, 'only accepted during dinner',
        ):
            ensure_preorder_slot_capacity(self.restaurant, self._local(12, 0))
        ensure_preorder_slot_capacity(self.restaurant, self._local(19, 0))

    def test_deactivating_a_meal_closes_it(self):
        self._add_period(MealPeriod.BREAKFAST, time(7, 0), time(10, 30))
        self._add_period(MealPeriod.LUNCH, time(12, 0), time(15, 30))
        self._add_period(MealPeriod.DINNER, time(18, 0), time(22, 0), active=False)

        self.assertNotIn(
            MealPeriod.DINNER,
            [window.kind for window in meal_windows_for(self.restaurant)],
        )
        with self.assertRaisesMessage(
            PreorderSlotUnavailable, 'only accepted during breakfast, lunch',
        ):
            ensure_preorder_slot_capacity(self.restaurant, self._local(19, 0))

    def test_all_meals_inactive_disables_preorders(self):
        self._add_period(MealPeriod.LUNCH, time(12, 0), time(15, 30), active=False)

        self.assertFalse(accepts_preorders(self.restaurant))
        self.assertEqual(
            upcoming_slot_days(self.restaurant, days=1, now=self.now), [],
        )
        with self.assertRaisesMessage(PreorderSlotUnavailable, 'not accepting'):
            ensure_preorder_slot_capacity(self.restaurant, self._local(12, 30))

    def test_meal_capacity_override_only_limits_that_meal(self):
        self._add_period(
            MealPeriod.LUNCH, time(12, 0), time(15, 30), capacity=1,
        )
        self._add_period(MealPeriod.DINNER, time(18, 0), time(22, 0))

        lunch = self._local(12, 0)
        self._book(lunch)

        with self.assertRaisesMessage(PreorderSlotUnavailable, 'fully booked'):
            ensure_preorder_slot_capacity(self.restaurant, lunch)
        # Dinner keeps the restaurant-wide capacity of 3.
        ensure_preorder_slot_capacity(self.restaurant, self._local(18, 0))

        day = upcoming_slot_days(self.restaurant, days=1, now=self.now)[0]
        capacities = {
            meal['type']: meal['slots'][0]['capacity'] for meal in day['meals']
        }
        self.assertEqual(capacities, {'LUNCH': 1, 'DINNER': 3})

    def test_zero_capacity_meal_is_not_offered(self):
        self._add_period(
            MealPeriod.LUNCH, time(12, 0), time(15, 30), capacity=0,
        )

        self.assertFalse(accepts_preorders(self.restaurant))
        with self.assertRaisesMessage(
            PreorderSlotUnavailable, 'Lunch pre-orders are not being accepted',
        ):
            ensure_preorder_slot_capacity(self.restaurant, self._local(12, 0))

    def test_slot_api_reports_meals_and_acceptance(self):
        payload = self._slots()

        self.assertTrue(payload['accepting_preorders'])
        self.assertEqual(
            [meal['type'] for meal in payload['meals']],
            ['BREAKFAST', 'LUNCH', 'DINNER'],
        )
        slots = [
            slot for day in payload['days'] for slot in day['slots']
        ]
        breakfast = next(
            slot for slot in slots if slot['meal_period'] == 'BREAKFAST'
        )
        self.assertEqual(breakfast['meal_label'], 'Breakfast')

    def _put_windows(self, user, meal_periods):
        client = APIClient()
        client.force_authenticate(user)
        return client.put(
            f'/api/restaurants/{self.restaurant.id}/meal-periods/',
            {'meal_periods': meal_periods},
            format='json',
        )

    def test_owner_can_replace_the_meal_windows(self):
        response = self._put_windows(self.owner, [
            {
                'kind': 'BREAKFAST', 'start_time': '08:00',
                'end_time': '10:00', 'is_active': False,
            },
            {
                'kind': 'LUNCH', 'start_time': '12:30', 'end_time': '15:00',
                'slot_capacity': 2, 'label': 'Midday',
            },
        ])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [
                (row['kind'], row['start_time'], row['slot_capacity'])
                for row in response.json()['meal_periods']
            ],
            [('BREAKFAST', '08:00:00', None), ('LUNCH', '12:30:00', 2)],
        )
        windows = meal_windows_for(self.restaurant)
        self.assertEqual(
            [(window.kind, window.start, window.end, window.label) for window in windows],
            [('LUNCH', time(12, 30), time(15, 0), 'Midday')],
        )

    def test_empty_payload_restores_the_platform_defaults(self):
        self._add_period(MealPeriod.DINNER, time(19, 0), time(21, 0))

        response = self._put_windows(self.owner, [])

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            MealPeriod.objects.filter(restaurant=self.restaurant).exists()
        )
        self.assertEqual(
            [window.kind for window in meal_windows_for(self.restaurant)],
            ['BREAKFAST', 'LUNCH', 'DINNER'],
        )

    def test_customers_cannot_edit_meal_windows(self):
        response = self._put_windows(self.customer, [
            {'kind': 'DINNER', 'start_time': '19:00', 'end_time': '21:00'},
        ])

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            MealPeriod.objects.filter(restaurant=self.restaurant).exists()
        )

    def test_overlapping_windows_are_rejected_without_touching_the_schedule(self):
        self._add_period(MealPeriod.DINNER, time(19, 0), time(21, 0))

        response = self._put_windows(self.owner, [
            {'kind': 'BREAKFAST', 'start_time': '08:00', 'end_time': '11:00'},
            {'kind': 'LUNCH', 'start_time': '10:30', 'end_time': '14:00'},
        ])

        self.assertEqual(response.status_code, 400)
        self.assertIn('overlap', response.json()['error'].lower())
        self.assertEqual(
            list(
                MealPeriod.objects.filter(restaurant=self.restaurant)
                .values_list('kind', flat=True)
            ),
            [MealPeriod.DINNER],
        )

    def test_start_time_must_precede_end_time(self):
        response = self._put_windows(self.owner, [
            {'kind': 'LUNCH', 'start_time': '15:00', 'end_time': '12:00'},
        ])

        self.assertEqual(response.status_code, 400)
        self.assertIn('start_time', response.json()['error'])

    def test_unknown_meal_kind_is_rejected(self):
        response = self._put_windows(self.owner, [
            {'kind': 'BRUNCH', 'start_time': '10:00', 'end_time': '12:00'},
        ])

        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid meal kind', response.json()['error'])

    # ── Pre-order cutoff ────────────────────────────────────────────────────

    def test_todays_meal_disappears_after_its_cutoff(self):
        self._add_period(
            MealPeriod.LUNCH, time(12, 0), time(15, 30),
            cutoff=time(11, 0),
        )

        # 11:30 is past lunch's 11:00 ordering deadline, so today is not
        # bookable at all and the first offered day is tomorrow.
        days = upcoming_slot_days(
            self.restaurant, days=2, now=self._local(11, 30),
        )

        self.assertEqual([day['date'] for day in days], ['2026-10-26'])
        self.assertIn(
            'LUNCH', [meal['type'] for meal in days[0]['meals']],
        )

    def test_cutoff_rejects_a_same_day_booking(self):
        self._add_period(
            MealPeriod.LUNCH, time(12, 0), time(15, 30),
            cutoff=time(11, 0),
        )

        with self.assertRaisesMessage(
            PreorderSlotUnavailable, 'Lunch pre-orders close at 11:00',
        ):
            ensure_preorder_slot_capacity(
                self.restaurant, self._local(12, 0),
                now=self._local(11, 30),
            )

    def test_cutoff_allows_a_booking_before_the_deadline(self):
        self._add_period(
            MealPeriod.LUNCH, time(12, 0), time(15, 30),
            cutoff=time(11, 0),
        )

        ensure_preorder_slot_capacity(
            self.restaurant, self._local(12, 0),
            now=self._local(10, 59),
        )

    def test_cutoff_does_not_block_future_days(self):
        self._add_period(
            MealPeriod.LUNCH, time(12, 0), time(15, 30),
            cutoff=time(11, 0),
        )

        ensure_preorder_slot_capacity(
            self.restaurant, self._local(12, 0, day=26),
            now=self._local(11, 30),
        )

    def test_meal_without_a_cutoff_stays_open(self):
        self._add_period(MealPeriod.LUNCH, time(12, 0), time(15, 30))

        ensure_preorder_slot_capacity(
            self.restaurant, self._local(14, 30),
            now=self._local(13, 0),
        )

    def test_owner_can_set_and_clear_a_cutoff(self):
        response = self._put_windows(self.owner, [
            {
                'kind': 'LUNCH', 'start_time': '12:00', 'end_time': '15:30',
                'preorder_cutoff_time': '11:00',
            },
        ])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()['meal_periods'][0]['preorder_cutoff_time'],
            '11:00:00',
        )
        self.assertEqual(
            meal_windows_for(self.restaurant)[0].preorder_cutoff,
            time(11, 0),
        )

        cleared = self._put_windows(self.owner, [
            {
                'kind': 'LUNCH', 'start_time': '12:00', 'end_time': '15:30',
                'preorder_cutoff_time': '',
            },
        ])

        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(
            cleared.json()['meal_periods'][0]['preorder_cutoff_time'],
        )

    def test_cutoff_after_the_window_closes_is_rejected(self):
        response = self._put_windows(self.owner, [
            {
                'kind': 'LUNCH', 'start_time': '12:00', 'end_time': '15:30',
                'preorder_cutoff_time': '16:00',
            },
        ])

        self.assertEqual(response.status_code, 400)
        self.assertIn('cutoff', response.json()['error'])
