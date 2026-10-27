"""Pre-order kitchen sheets: generation at cut-off, scoping and download."""

from datetime import date, datetime, time
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from apps.orders.models import Order, OrderItem, PreorderSheet
from apps.products.models import Food
from apps.restaurants.models import MealPeriod, Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from domain.services.orders.preorder_sheets import generate_due_sheets
from rest_framework.test import APIClient

POINT = Point(39.2083, -6.7924)
TZ = ZoneInfo('Africa/Dar_es_Salaam')

def _results(response):
    payload = response.json()
    return payload['results'] if isinstance(payload, dict) else payload


class PreorderSheetTests(TestCase):
    def setUp(self):
        self.service_date = date(2026, 10, 27)
        # Lunch bookings close at 11:00 local on the service day.
        self.before_cutoff = datetime(2026, 10, 27, 10, 30, tzinfo=TZ)
        self.after_cutoff = datetime(2026, 10, 27, 11, 5, tzinfo=TZ)

        self.owner = User.objects.create_user(
            username='owner_sheets', phone='+255700002001',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=self.owner,
            name='Sheet Kitchen',
            location='Main Hall',
            geo_location=POINT,
            average_prep_time=25,
        )
        MealPeriod.objects.create(
            restaurant=self.restaurant,
            kind=MealPeriod.LUNCH,
            start_time=time(12, 0),
            end_time=time(15, 30),
            preorder_cutoff_time=time(11, 0),
        )
        self.section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        self.food = Food.objects.create(
            section=self.section, name='Kuku wa kupaka',
            price=Decimal('5000.00'),
        )

        self.customer = User.objects.create_user(
            username='sheet_customer', phone='+255700002002',
            password='password123', role='customer',
        )

    def _order(
        self,
        scheduled_for,
        quantity=2,
        status='SCHEDULED',
        is_paid=True,
        notes=None,
    ):
        order = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status=status,
            is_paid=is_paid,
            items_subtotal=Decimal('5000.00') * quantity,
            total_amount=Decimal('5000.00') * quantity,
            scheduled_for=scheduled_for,
            notes=notes,
        )
        OrderItem.objects.create(
            order=order, food=self.food, quantity=quantity,
            price=Decimal('5000.00'),
        )
        return order

    def _local(self, hour, minute=0):
        return datetime(
            self.service_date.year, self.service_date.month,
            self.service_date.day, hour, minute, tzinfo=TZ,
        )

    def test_sheet_is_created_once_bookings_close(self):
        self._order(self._local(12, 30), quantity=2)
        self._order(self._local(13, 0), quantity=1)

        created = generate_due_sheets(now=self.after_cutoff, notify=False)

        self.assertEqual(len(created), 1)
        sheet = created[0]
        self.assertEqual(sheet.order_count, 2)
        self.assertEqual(sheet.item_count, 3)
        self.assertEqual(sheet.subtotal, Decimal('15000.00'))
        self.assertEqual(sheet.meal_period, MealPeriod.LUNCH)
        self.assertEqual(sheet.filename, 'preorders-2026-10-27-lunch.pdf')

        self.assertEqual(bytes(sheet.pdf)[:4], b'%PDF')

    def test_nothing_is_created_before_the_cutoff(self):
        self._order(self._local(12, 30))

        self.assertEqual(
            generate_due_sheets(now=self.before_cutoff, notify=False), [],
        )
        self.assertFalse(PreorderSheet.objects.exists())

    def test_a_second_run_does_not_duplicate_the_sheet(self):
        self._order(self._local(12, 30))

        generate_due_sheets(now=self.after_cutoff, notify=False)
        again = generate_due_sheets(now=self.after_cutoff, notify=False)

        self.assertEqual(again, [])
        self.assertEqual(PreorderSheet.objects.count(), 1)

    def test_unpaid_cancelled_and_other_meals_are_left_out(self):
        self._order(self._local(12, 30), quantity=2)
        self._order(self._local(12, 45), is_paid=False, status='PENDING_PAYMENT')
        self._order(self._local(13, 0), status='CANCELLED')
        self._order(self._local(19, 0))  # dinner, outside the lunch window

        created = generate_due_sheets(now=self.after_cutoff, notify=False)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].order_count, 1)
        self.assertEqual(created[0].item_count, 2)

    def test_no_sheet_when_the_meal_has_no_bookings(self):
        self.assertEqual(
            generate_due_sheets(now=self.after_cutoff, notify=False), [],
        )
        self.assertFalse(PreorderSheet.objects.exists())

    @patch('infrastructure.notifications.service.NotificationService.send_push')
    def test_the_owner_is_told_the_sheet_is_ready(self, send_push):
        self._order(self._local(12, 30))

        generate_due_sheets(now=self.after_cutoff)

        send_push.assert_called_once()
        kwargs = send_push.call_args.kwargs
        self.assertEqual(kwargs['user_id'], str(self.owner.id))
        self.assertIn('Lunch', kwargs['title'])
        self.assertEqual(kwargs['data']['type'], 'preorder_sheet_ready')


class PreorderSheetApiTests(TestCase):
    def setUp(self):
        self.service_date = date(2026, 10, 27)
        self.after_cutoff = datetime(2026, 10, 27, 11, 5, tzinfo=TZ)

        self.owner = User.objects.create_user(
            username='owner_sheets_api', phone='+255700002010',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=self.owner,
            name='API Sheet Kitchen',
            location='Main Hall',
            geo_location=POINT,
        )
        MealPeriod.objects.create(
            restaurant=self.restaurant,
            kind=MealPeriod.LUNCH,
            start_time=time(12, 0),
            end_time=time(15, 30),
            preorder_cutoff_time=time(11, 0),
        )
        section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        self.food = Food.objects.create(
            section=section, name='Wali Maharage', price=Decimal('4000.00'),
        )
        self.customer = User.objects.create_user(
            username='sheet_customer_api', phone='+255700002011',
            password='password123', role='customer',
        )

        order = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='SCHEDULED',
            is_paid=True,
            items_subtotal=Decimal('8000.00'),
            total_amount=Decimal('8000.00'),
            scheduled_for=datetime(2026, 10, 27, 12, 30, tzinfo=TZ),
        )
        OrderItem.objects.create(
            order=order, food=self.food, quantity=2, price=Decimal('4000.00'),
        )

        self.sheet = generate_due_sheets(now=self.after_cutoff, notify=False)[0]
        self.client = APIClient()

    def test_vendor_lists_only_their_sheets(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.get('/api/preorder-sheets/')

        self.assertEqual(response.status_code, 200)
        rows = _results(response)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], str(self.sheet.id))
        self.assertEqual(rows[0]['restaurant_name'], 'API Sheet Kitchen')
        self.assertEqual(rows[0]['order_count'], 1)

    def test_a_different_vendor_sees_nothing(self):
        stranger = User.objects.create_user(
            username='other_vendor', phone='+255700002012',
            password='password123', role='vendor',
        )
        self.client.force_authenticate(user=stranger)

        self.assertEqual(_results(self.client.get('/api/preorder-sheets/')), [])
        detail = self.client.get(f'/api/preorder-sheets/{self.sheet.id}/')
        self.assertEqual(detail.status_code, 404)

    def test_download_link_serves_the_pdf_without_a_bearer_token(self):
        self.client.force_authenticate(user=self.owner)
        link = self.client.get(
            f'/api/preorder-sheets/{self.sheet.id}/download-link/',
        )

        self.assertEqual(link.status_code, 200)
        payload = link.json()
        self.assertEqual(payload['filename'], 'preorders-2026-10-27-lunch.pdf')
        self.assertGreater(payload['expires_in'], 0)

        parsed = urlparse(payload['url'])
        anonymous = APIClient()
        response = anonymous.get(f'{parsed.path}?{parsed.query}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('attachment', response['Content-Disposition'])
        body = b''.join(response.streaming_content)
        self.assertEqual(body[:4], b'%PDF')

    def test_a_tampered_token_is_rejected(self):
        parsed = urlparse(
            f'/api/preorder-sheets/{self.sheet.id}/download/',
        )
        response = APIClient().get(f'{parsed.path}?token=not-a-real-token')

        self.assertEqual(response.status_code, 403)
