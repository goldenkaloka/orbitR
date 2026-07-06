"""
Tests that ReleasePaymentService correctly distributes batch driver_payout
proportionally across orders in a batch.

Creates a DeliveryBatch with 3 orders + separate Payment records,
calls ReleasePaymentService.execute() for each, then asserts that:
  - The restaurant owner receives (items_subtotal * (1 - commission)) per order
  - The driver receives (batch.driver_payout / N) per order
  - After all payments are released, driver balance == batch.driver_payout

Run:
    python manage.py test tests.e2e.test_batch_payout --verbosity=2
"""

import logging
from decimal import Decimal

from apps.delivery.models import Driver
from apps.orders.models import DeliveryBatch, Order, OrderItem
from apps.payments.models import Payment
from apps.pricing.models import PricingPolicy
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from apps.wallets.models import Wallet
from django.contrib.gis.geos import Point
from django.test import TestCase

logger = logging.getLogger(__name__)
logging.getLogger('domain.services.payment_services').setLevel(logging.DEBUG)
logging.getLogger('domain.services.wallet_services').setLevel(logging.DEBUG)


class BatchPaymentDistributionTest(TestCase):
    """ReleasePaymentService correctly splits batch.driver_payout across orders."""

    BATCH_COUNT = 3
    BATCH_DRIVER_PAYOUT = Decimal('6000.00')

    def setUp(self):
        self.restaurant_owner = User.objects.create_user(
            username='batch_owner', phone='+255700000021', password='pass',
            role='vendor',
        )
        self.driver_user = User.objects.create_user(
            username='batch_driver', phone='+255700000022', password='pass',
            role='driver',
        )
        self.customers = [
            User.objects.create_user(
                username=f'batch_cust_{i}', phone=f'+25570000003{i}', password='pass',
                role='customer',
            )
            for i in range(self.BATCH_COUNT)
        ]

        self.driver = Driver.objects.create(
            user=self.driver_user, status='AVAILABLE',
            location=Point(39.2083, -6.7924), driver_type='platform',
        )

        self.restaurant = Restaurant.objects.create(
            owner=self.restaurant_owner, name="Batch Restaurant",
            location="Loc", geo_location=Point(39.2083, -6.7924),
            max_delivery_radius_km=10.0, commission_rate=Decimal('0.15'),
        )
        section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name="Main", description="",
        )
        food = Food.objects.create(
            section=section, name="Batch Food", price=Decimal('5000.00'),
        )

        PricingPolicy.objects.create(
            name="Test", is_active=True,
            base_fee=Decimal('1000.00'), per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'), driver_min_guarantee=Decimal('2000.00'),
            driver_per_km_rate=Decimal('500.00'), driver_per_stop_rate=Decimal('300.00'),
        )

        self.batch = DeliveryBatch.objects.create(
            restaurant=self.restaurant, driver=self.driver,
            status='DISPATCHED', driver_payout=self.BATCH_DRIVER_PAYOUT,
            user_paid_total=Decimal('9000.00'),
        )

        self.orders = []
        self.payments = []
        for i in range(self.BATCH_COUNT):
            order = Order.objects.create(
                customer=self.customers[i], restaurant=self.restaurant,
                delivery_address=f"Addr {i}", driver=self.driver,
                batch=self.batch,
                delivery_location=Point(39.2100 + i * 0.01, -6.7930),
                items_subtotal=Decimal('10000.00'), delivery_fee=Decimal('3000.00'),
                total_amount=Decimal('13300.00'), status='PICKED',
                delivery_otp=f'12{i}34',
            )
            OrderItem.objects.create(
                order=order, food=food, quantity=2, price=Decimal('5000.00'),
            )
            self.orders.append(order)

            payment = Payment.objects.create(
                payable_type='order', payable_id=str(order.id),
                order=order, amount=order.total_amount,
                status='COLLECTED',
                provider_reference=f'BATCH_TXN_{i:03d}',
            )
            self.payments.append(payment)

        self._log_wallet(self.restaurant_owner, 'Before')
        self._log_wallet(self.driver_user, 'Before')

    def _log_wallet(self, user, label):
        wallet, _ = Wallet.objects.get_or_create(user=user)
        logger.info("  WALLET [%s]: %s  balance=%s", label, user.username, wallet.balance)

    def _balance(self, user) -> Decimal:
        wallet, _ = Wallet.objects.get_or_create(user=user)
        return wallet.balance

    def test_batch_credits_restaurant_and_driver(self):
        from domain.services.payment_services import ReleasePaymentService

        expected_per_order_driver = (self.BATCH_DRIVER_PAYOUT / self.BATCH_COUNT).quantize(Decimal('0.01'))
        logger.info("\n--- Releasing %d batch payments ---", self.BATCH_COUNT)

        for i, payment in enumerate(self.payments):
            logger.info("Releasing payment %d/%d", i + 1, self.BATCH_COUNT)
            ReleasePaymentService.execute(payment.id)
            payment.refresh_from_db()
            self.assertEqual(payment.status, 'DISBURSED')

            driver_bal = self._balance(self.driver_user)
            expected_so_far = expected_per_order_driver * (i + 1)
            self.assertEqual(driver_bal, expected_so_far,
                             f"Driver balance after payment {i+1}: expected {expected_so_far}, got {driver_bal}")

        self._log_wallet(self.restaurant_owner, 'After')
        self._log_wallet(self.driver_user, 'After')

        commission = Decimal('0.15')
        expected_per_order_restaurant = (Decimal('10000.00') * (Decimal('1.0') - commission)).quantize(Decimal('0.01'))
        expected_restaurant_total = expected_per_order_restaurant * self.BATCH_COUNT
        rest_bal = self._balance(self.restaurant_owner)
        self.assertEqual(rest_bal, expected_restaurant_total,
                         f"Restaurant owner: expected {expected_restaurant_total}, got {rest_bal}")

        driver_bal = self._balance(self.driver_user)
        self.assertEqual(driver_bal, self.BATCH_DRIVER_PAYOUT,
                         f"Driver: expected {self.BATCH_DRIVER_PAYOUT}, got {driver_bal}")

        logger.info(">>> BATCH DISTRIBUTION TEST PASSED <<<")
