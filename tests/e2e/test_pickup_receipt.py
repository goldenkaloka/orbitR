"""Counter receipt PDF for pickup orders.

The launch case is an admin-owned kitchen: the platform runs the restaurant,
so there is no vendor app at the counter and the customer's PDF is the
document staff match against the order code. The same link also serves
vendor-run kitchens, which is why the pay-at-counter variant is covered.
"""

from decimal import Decimal
from urllib.parse import urlsplit

from apps.orders.models import Order, OrderItem
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from domain.services.orders.pickup_receipt import (
    payment_label,
    receipt_code,
    receipt_filename,
)
from rest_framework.test import APIClient

RESTAURANT_POINT = Point(39.2083, -6.7924)


class PickupReceiptTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username='admin_kitchen', phone='+255700000031',
            password='password123', role='admin',
        )
        self.customer = User.objects.create_user(
            username='Neema', phone='+255700000032',
            password='password123', role='customer',
        )
        self.other_customer = User.objects.create_user(
            username='Baraka', phone='+255700000033',
            password='password123', role='customer',
        )
        self.restaurant = Restaurant.objects.create(
            owner=self.admin,
            name='OrbitR Campus Kitchen',
            location='Main Hall',
            geo_location=RESTAURANT_POINT,
            settlement_mode=Restaurant.SETTLEMENT_COMPANY_OWNED,
        )
        section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        food = Food.objects.create(
            section=section, name='Chips Mayai', price=Decimal('4000.00'),
        )
        self.order = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='READY',
            fulfillment_type=Order.FULFILLMENT_PICKUP,
            delivery_address='Pickup at Main Hall',
            items_subtotal=Decimal('8000.00'),
            delivery_fee=Decimal('0.00'),
            service_fee=Decimal('500.00'),
            total_amount=Decimal('8500.00'),
            is_paid=True,
        )
        OrderItem.objects.create(
            order=self.order, food=food,
            quantity=2, price=Decimal('4000.00'),
        )

    def _link(self, user=None):
        self.client.force_authenticate(user or self.customer)
        return self.client.get(f'/api/orders/{self.order.id}/pickup-receipt/')

    def _download(self, url):
        parsed = urlsplit(url)
        self.client.force_authenticate(None)
        return self.client.get(f'{parsed.path}?{parsed.query}')

    # ── Code and filename ────────────────────────────────────────────────────

    def test_receipt_code_matches_the_kitchen_ticket_convention(self):
        self.assertEqual(receipt_code(self.order), f'ORD-{str(self.order.id)[:6].upper()}')
        self.assertEqual(
            receipt_filename(self.order),
            f'pickup-receipt-{receipt_code(self.order)}.pdf',
        )

    # ── Link minting ─────────────────────────────────────────────────────────

    def test_customer_gets_a_signed_receipt_link(self):
        response = self._link()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn(f'/api/orders/{self.order.id}/pickup-receipt/download/', body['url'])
        self.assertIn('token=', body['url'])
        self.assertEqual(body['filename'], receipt_filename(self.order))
        self.assertEqual(body['expires_in'], 900)

    def test_admin_running_the_kitchen_can_mint_the_link(self):
        response = self._link(self.admin)

        self.assertEqual(response.status_code, 200)

    def test_another_customer_cannot_mint_the_link(self):
        response = self._link(self.other_customer)

        self.assertEqual(response.status_code, 404)

    def test_delivery_orders_have_no_counter_receipt(self):
        delivery = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='READY',
            fulfillment_type=Order.FULFILLMENT_DELIVERY,
            delivery_address='Msasani',
            is_paid=True,
        )

        self.client.force_authenticate(self.customer)
        response = self.client.get(f'/api/orders/{delivery.id}/pickup-receipt/')

        self.assertEqual(response.status_code, 400)

    # ── Download ─────────────────────────────────────────────────────────────

    def test_download_serves_a_pdf_attachment(self):
        url = self._link().json()['url']

        response = self._download(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertIn(receipt_filename(self.order), response['Content-Disposition'])
        body = b''.join(response.streaming_content)
        self.assertTrue(body.startswith(b'%PDF'))

    def test_link_for_one_order_cannot_open_another(self):
        url = self._link().json()['url']
        other = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='READY',
            fulfillment_type=Order.FULFILLMENT_PICKUP,
            delivery_address='Pickup at Main Hall',
            is_paid=True,
        )
        parsed = urlsplit(url)

        response = self._download(
            f'/api/orders/{other.id}/pickup-receipt/download/?{parsed.query}'
        )

        self.assertEqual(response.status_code, 403)

    def test_tampered_token_is_rejected(self):
        response = self._download(
            f'/api/orders/{self.order.id}/pickup-receipt/download/?token=broken'
        )

        self.assertEqual(response.status_code, 403)

    # ── Admin-owned payment semantics ────────────────────────────────────────

    def test_paid_pickup_reads_as_paid(self):
        self.assertEqual(payment_label(self.order), ('PAID', True))

    def test_company_owned_pickup_owes_nothing_at_the_counter(self):
        self.order.is_paid = False
        self.order.save(update_fields=['is_paid'])

        self.assertEqual(
            payment_label(self.order),
            ('NO PAYMENT DUE AT THE COUNTER', True),
        )

    def test_vendor_run_pickup_still_pays_at_the_counter(self):
        self.order.is_paid = False
        self.order.save(update_fields=['is_paid'])
        self.restaurant.settlement_mode = Restaurant.SETTLEMENT_VENDOR_PAYOUT
        self.restaurant.save(update_fields=['settlement_mode'])

        self.assertEqual(
            payment_label(self.order), ('PAY AT THE COUNTER', False),
        )
