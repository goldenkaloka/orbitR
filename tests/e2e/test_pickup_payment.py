"""
Pickup payment: the platform pays the restaurant at the counter.

The two properties that matter most are covered here:
  1. idempotency — a repeated tap must never produce a second real payout
  2. no double payment — a restaurant paid at pickup must not be paid again
     when the order is delivered
"""

from decimal import Decimal
from unittest.mock import patch

from apps.delivery.models import Driver
from apps.orders.models import Order
from apps.payments.models import PickupDisbursement
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from apps.wallets.models import Wallet
from django.contrib.gis.geos import Point
from django.test import TestCase
from rest_framework.test import APIClient, override_settings

RESTAURANT_POINT = Point(39.2083, -6.7924)


@override_settings(MOCK_PAYMENTS=True)
class PayRestaurantAtPickupTests(TestCase):
    def setUp(self):
        # force_authenticate() lives on DRF's client, not Django's.
        self.client = APIClient()
        self.driver_user = User.objects.create_user(
            username='rider1', phone='+255700000021',
            password='password123', role='driver',
        )
        self.driver = Driver.objects.create(
            user=self.driver_user,
            status='DELIVERING',
            driver_type='platform',
            location=RESTAURANT_POINT,
        )
        self.customer = User.objects.create_user(
            username='cust1', phone='+255700000022',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='owner1', phone='+255700000023',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Pickup Cafe',
            location='Main Hall',
            geo_location=RESTAURANT_POINT,
            commission_rate=Decimal('0.050'),
        )
        section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main',
        )
        Food.objects.create(
            section=section, name='Burger', price=Decimal('10000.00'),
        )

        self.order = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            # A driver is already attached, so the order is past READY —
            # order_assignment moves it to ASSIGNED the moment a driver takes it.
            status='ASSIGNED',
            driver=self.driver,
            items_subtotal=Decimal('10000.00'),
            delivery_fee=Decimal('1500.00'),
            service_fee=Decimal('500.00'),
            total_amount=Decimal('12000.00'),
        )

        # The customer already paid on checkout, so escrow holds the money.
        from domain.services.payment_services import _hold_escrow
        _hold_escrow(Decimal('12000.00'))

    def _pay(self, account='0712345678', network='M_PESA'):
        from domain.services.orders.pickup_payment import (
            PayRestaurantAtPickupService,
        )
        return PayRestaurantAtPickupService.execute(
            order_id=str(self.order.id),
            actor=self.driver_user,
            account_number=account,
            network=network,
        )

    # ── Amount ────────────────────────────────────────────────────────────────

    def test_amount_is_net_of_commission(self):
        from domain.services.orders.pickup_payment import restaurant_net_share

        # 10,000 less 5% commission = 9,500
        self.assertEqual(
            restaurant_net_share(self.order), Decimal('9500.00'),
        )

    # ── Happy path ────────────────────────────────────────────────────────────

    def test_successful_payment_records_disbursement(self):
        disbursement = self._pay()

        self.assertEqual(disbursement.status, PickupDisbursement.STATUS_SUCCESS)
        self.assertEqual(disbursement.amount, Decimal('9500.00'))
        self.assertEqual(disbursement.account_number, '0712345678')
        self.assertEqual(disbursement.network, 'M_PESA')
        self.assertTrue(disbursement.gateway_reference)

        self.order.refresh_from_db()
        self.assertEqual(self.order.pickup_paid_amount, Decimal('9500.00'))

    def test_payment_debits_escrow(self):
        from domain.services.payment_services import _get_platform_wallet

        before = _get_platform_wallet().pending_balance
        self._pay()
        after = _get_platform_wallet().pending_balance

        self.assertEqual(before - after, Decimal('9500.00'))

    def test_company_owned_restaurant_needs_no_payout_to_pick_up(self):
        """Company-owned restaurants are internal; no external payout exists."""
        self.restaurant.settlement_mode = Restaurant.SETTLEMENT_COMPANY_OWNED
        self.restaurant.save(update_fields=['settlement_mode'])

        from domain.services.orders.order_fulfillment import PickupOrderService

        PickupOrderService.execute(
            str(self.order.id), self.driver_user, bypass_geofence=True,
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'PICKED')
        self.assertFalse(
            PickupDisbursement.objects.filter(order=self.order).exists()
        )

    def test_company_owned_restaurant_rejects_manual_payout(self):
        self.restaurant.settlement_mode = Restaurant.SETTLEMENT_COMPANY_OWNED
        self.restaurant.save(update_fields=['settlement_mode'])

        with self.assertRaisesMessage(ValueError, 'company-owned'):
            self._pay()

    # ── Idempotency — the important one ───────────────────────────────────────

    def test_repeated_payment_does_not_call_the_gateway_twice(self):
        """
        ClickPesa appends a random suffix to every payout reference, so a second
        gateway call would be a second real transfer. Idempotency has to come
        from the database, not the reference string.
        """
        first = self._pay()

        with patch(
            'domain.services.orders.pickup_payment._call_gateway'
        ) as gateway:
            second = self._pay()

        gateway.assert_not_called()
        self.assertEqual(first.id, second.id)

        self.assertEqual(PickupDisbursement.objects.count(), 1)

        self.order.refresh_from_db()
        self.assertEqual(self.order.pickup_paid_amount, Decimal('9500.00'))

    # ── Failure restores escrow ───────────────────────────────────────────────

    def test_failed_payment_restores_escrow_and_allows_retry(self):
        before = None
        from domain.services.payment_services import _get_platform_wallet
        before = _get_platform_wallet().pending_balance

        with patch(
            'domain.services.orders.pickup_payment._call_gateway',
            return_value={'success': False, 'error': 'gateway down'},
        ):
            disbursement = self._pay()

        self.assertEqual(disbursement.status, PickupDisbursement.STATUS_FAILED)
        self.assertEqual(_get_platform_wallet().pending_balance, before)

        self.order.refresh_from_db()
        self.assertEqual(self.order.pickup_paid_amount, Decimal('0.00'))

    # ── Geofence ─────────────────────────────────────────────────────────────

    def test_payment_outside_the_geofence_is_refused(self):
        """The driver must be at the restaurant — same radius as pickup."""
        # ~2km away, well beyond the 300m pickup radius.
        self.driver.location = Point(39.2263, -6.7924)
        self.driver.save(update_fields=['location'])
        self.driver.refresh_from_db()

        with self.assertRaises(ValueError) as ctx:
            self._pay()
        self.assertIn('Pay the restaurant once you are there', str(ctx.exception))

        self.assertEqual(PickupDisbursement.objects.count(), 0)

    # ── Authorisation ────────────────────────────────────────────────────────

    def test_only_the_assigned_driver_can_pay(self):
        other_user = User.objects.create_user(
            username='rider2', phone='+255700000024',
            password='password123', role='driver',
        )
        Driver.objects.create(
            user=other_user, status='DELIVERING',
            driver_type='platform', location=RESTAURANT_POINT,
        )

        with self.assertRaises(PermissionError):
            from domain.services.orders.pickup_payment import (
                PayRestaurantAtPickupService,
            )
            PayRestaurantAtPickupService.execute(
                order_id=str(self.order.id),
                actor=other_user,
                account_number='0712345678',
            )

    def test_non_driver_cannot_pay(self):
        with self.assertRaises(PermissionError):
            from domain.services.orders.pickup_payment import (
                PayRestaurantAtPickupService,
            )
            PayRestaurantAtPickupService.execute(
                order_id=str(self.order.id),
                actor=self.customer,
                account_number='0712345678',
            )

    def test_cannot_pay_an_order_that_is_not_ready(self):
        self.order.status = 'PREPARING'
        self.order.save(update_fields=['status'])

        with self.assertRaises(ValueError):
            self._pay()

    # ── Pickup gate ──────────────────────────────────────────────────────────

    def test_pickup_is_refused_before_the_restaurant_is_paid(self):
        from domain.services.orders.order_fulfillment import PickupOrderService

        with self.assertRaises(ValueError) as ctx:
            PickupOrderService.execute(str(self.order.id), self.driver_user)
        self.assertIn('Pay the restaurant', str(ctx.exception))

    def test_pickup_succeeds_once_paid(self):
        from domain.services.orders.order_fulfillment import PickupOrderService

        self._pay()
        order = PickupOrderService.execute(str(self.order.id), self.driver_user)

        self.assertEqual(order.status, 'PICKED')

    # ── No double payment on delivery ────────────────────────────────────────

    def test_delivery_does_not_pay_the_restaurant_twice(self):
        """
        The restaurant already holds real cash from the pickup payout. Delivery
        release must settle only the driver and the platform.
        """
        from apps.payments.models import Payment
        from apps.wallets.models import Wallet
        from domain.services.orders.pickup_payment import restaurant_net_share
        from domain.services.payment_services import ReleasePaymentService

        self._pay()

        # What delivery-time release would have paid the restaurant.
        expected_restaurant_payout = restaurant_net_share(self.order)
        self.assertEqual(expected_restaurant_payout, Decimal('9500.00'))

        # Staff wallet so we can prove no second credit lands there.
        section_wallet = Wallet.objects.create(section=self.restaurant.sections.first())

        payment = Payment.objects.create(
            payable_type='order',
            payable_id=str(self.order.id),
            amount=Decimal('12000.00'),
            status='COLLECTED',
        )

        ReleasePaymentService.execute(payment.id)

        self.assertEqual(
            section_wallet.transactions.count(), 0,
            "Restaurant was already paid at pickup — must not be credited again.",
        )

        # The escrow must be fully drained: pickup took 9,500, delivery the rest.
        from domain.services.payment_services import _get_platform_wallet
        self.assertEqual(
            _get_platform_wallet().pending_balance, Decimal('0.00'),
        )

    def test_delivery_still_pays_the_driver(self):
        from apps.payments.models import Payment
        from domain.services.payment_services import ReleasePaymentService

        self._pay()

        driver_wallet_before = Wallet.objects.filter(
            user=self.driver_user,
        ).first()
        before = driver_wallet_before.balance if driver_wallet_before else Decimal('0')

        payment = Payment.objects.create(
            payable_type='order',
            payable_id=str(self.order.id),
            amount=Decimal('12000.00'),
            status='COLLECTED',
        )
        ReleasePaymentService.execute(payment.id)

        driver_wallet = Wallet.objects.get(user=self.driver_user)
        self.assertGreater(driver_wallet.balance, before)

    # ── Endpoint ─────────────────────────────────────────────────────────────

    def test_pay_restaurant_endpoint_validates_the_account_number(self):
        self.client.force_authenticate(self.driver_user)
        response = self.client.post(
            f'/api/orders/{self.order.id}/pay-restaurant/',
            {'account_number': 'abc'},
            format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'INVALID_ACCOUNT')

    def test_pickup_payment_endpoint_reports_the_amount(self):
        self.client.force_authenticate(self.driver_user)
        response = self.client.get(f'/api/orders/{self.order.id}/pickup-payment/')

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['amount'], '9500.00')
        self.assertFalse(body['is_paid'])

    def test_concierge_confirm_price_difference_refunds_customer(self):
        from domain.services.orders.concierge import ConciergeOrderService
        from apps.wallets.models import Wallet

        admin_user = User.objects.create_user(
            username='admin_test', phone='+255700000099',
            password='password123', role='admin',
        )
        self.restaurant.settlement_mode = Restaurant.SETTLEMENT_PLATFORM_PREPAID
        self.restaurant.source_type = Restaurant.SOURCE_PLATFORM_MANAGED
        self.restaurant.save()

        self.order.status = 'AWAITING_CONFIRMATION'
        self.order.is_paid = True
        self.order.confirmation_status = Order.CONFIRMATION_PENDING
        self.order.total_amount = Decimal('12000.00')
        self.order.save()

        # Create customer payment record in COLLECTED status
        from apps.payments.models import Payment
        payment = Payment.objects.create(
            payable_type='order',
            payable_id=str(self.order.id),
            amount=Decimal('12000.00'),
            status='COLLECTED',
            provider_reference='TEST_REF_123',
        )

        # Confirm with lower bill: 10,000 instead of 12,000 (diff = 2,000)
        ConciergeOrderService.confirm(
            order_id=str(self.order.id),
            actor=admin_user,
            confirmed_total=Decimal('10000.00'),
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'CONFIRMED')
        self.assertEqual(self.order.confirmed_total, Decimal('10000.00'))

        payment.refresh_from_db()
        self.assertEqual(payment.status, 'PARTIALLY_REFUNDED')

    def test_concierge_timeout_cancels_and_refunds_stale_order(self):
        from datetime import timedelta
        from django.utils import timezone
        from apps.orders.tasks import concierge_confirmation_timeout_check
        from apps.payments.models import Payment

        self.order.status = 'AWAITING_CONFIRMATION'
        self.order.is_paid = True
        self.order.confirmation_status = Order.CONFIRMATION_PENDING
        self.order.created_at = timezone.now() - timedelta(minutes=15)
        self.order.save()

        Payment.objects.create(
            payable_type='order',
            payable_id=str(self.order.id),
            amount=Decimal('12000.00'),
            status='COLLECTED',
            provider_reference='TIMEOUT_REF_123',
        )

        result = concierge_confirmation_timeout_check()
        self.assertIn('Auto-cancelled 1', result)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'CANCELLED')
        self.assertEqual(self.order.cancellation_category, 'system_timeout')