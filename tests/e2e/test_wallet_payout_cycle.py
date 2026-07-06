"""
Tests that ReleasePaymentService correctly distributes money to wallets.

Creates order + payment directly, calls ReleasePaymentService.execute(),
then asserts restaurant owner and driver wallets are credited.

Run:
    python manage.py test tests.e2e.test_wallet_payout_cycle --verbosity=2
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


class ReleasePaymentWalletTest(TestCase):
    """Call ReleasePaymentService directly and verify wallet credit amounts."""

    def setUp(self):
        self.restaurant_owner = User.objects.create_user(
            username='rest_owner', phone='+255700000011', password='pass',
            role='vendor',
        )
        self.driver_user = User.objects.create_user(
            username='driver_user', phone='+255700000012', password='pass',
            role='driver',
        )
        self.customer = User.objects.create_user(
            username='cust', phone='+255700000010', password='pass',
            role='customer',
        )

        self.driver = Driver.objects.create(
            user=self.driver_user, status='AVAILABLE',
            location=Point(39.2083, -6.7924), driver_type='platform',
        )

        self.restaurant = Restaurant.objects.create(
            owner=self.restaurant_owner, name="Test Restaurant",
            location="Loc", geo_location=Point(39.2083, -6.7924),
            max_delivery_radius_km=10.0, commission_rate=Decimal('0.10'),
        )
        self.section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name="Main", description="",
        )
        self.food = Food.objects.create(
            section=self.section, name="Test Food", price=Decimal('5000.00'),
        )

        PricingPolicy.objects.create(
            name="Test", is_active=True,
            base_fee=Decimal('1000.00'), per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'), driver_min_guarantee=Decimal('2000.00'),
            driver_per_km_rate=Decimal('500.00'), driver_per_stop_rate=Decimal('300.00'),
        )

        self.order = Order.objects.create(
            customer=self.customer, restaurant=self.restaurant,
            delivery_address="Addr", driver=self.driver,
            delivery_location=Point(39.2100, -6.7930),
            items_subtotal=Decimal('10000.00'), delivery_fee=Decimal('3000.00'),
            total_amount=Decimal('13300.00'), status='PICKED',
        )
        OrderItem.objects.create(
            order=self.order, food=self.food,
            quantity=2, price=Decimal('5000.00'),
        )
        # Set delivery_otp so DeliverOrderService can complete
        self.order.delivery_otp = '1234'
        self.order.save(update_fields=['delivery_otp'])

        self.payment = Payment.objects.create(
            payable_type='order', payable_id=str(self.order.id),
            order=self.order, amount=self.order.total_amount,
            status='COLLECTED',
            provider_reference='TEST_TXN_001',
        )

        self._log_wallet(self.restaurant_owner, 'Before')
        self._log_wallet(self.driver_user, 'Before')

    def _log_wallet(self, user, label):
        wallet, _ = Wallet.objects.get_or_create(user=user)
        logger.info("  WALLET [%s]: %s  balance=%s", label, user.username, wallet.balance)

    def _balance(self, user) -> Decimal:
        wallet, _ = Wallet.objects.get_or_create(user=user)
        return wallet.balance

    def test_release_credits_restaurant_and_driver_wallets(self):
        from domain.services.payment_services import ReleasePaymentService

        logger.info("\n--- Calling ReleasePaymentService.execute() ---")
        ReleasePaymentService.execute(self.payment.id)

        self.payment.refresh_from_db()
        logger.info("Payment status: %s", self.payment.status)
        self.assertEqual(self.payment.status, 'DISBURSED')

        self._log_wallet(self.restaurant_owner, 'After')
        self._log_wallet(self.driver_user, 'After')

        commission = Decimal('0.10')
        expected_restaurant = (Decimal('10000.00') * (Decimal('1.0') - commission)).quantize(Decimal('0.01'))
        expected_driver = (Decimal('3000.00') * Decimal('0.90')).quantize(Decimal('0.01'))

        rest_bal = self._balance(self.restaurant_owner)
        driver_bal = self._balance(self.driver_user)

        logger.info("Expected restaurant: %s  actual: %s", expected_restaurant, rest_bal)
        logger.info("Expected driver:      %s  actual: %s", expected_driver, driver_bal)

        self.assertEqual(rest_bal, expected_restaurant,
                         f"Restaurant owner wallet: expected {expected_restaurant}, got {rest_bal}")
        self.assertEqual(driver_bal, expected_driver,
                         f"Driver wallet: expected {expected_driver}, got {driver_bal}")

        logger.info(">>> WALLET DISTRIBUTION TEST PASSED <<<")


class BatchPaymentDistributionTest(TestCase):
    """Test that ReleasePaymentService correctly distributes batch driver_payout."""

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
            for i in range(3)
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

        # Create batch
        self.batch = DeliveryBatch.objects.create(
            restaurant=self.restaurant, driver=self.driver,
            status='DISPATCHED', driver_payout=Decimal('6000.00'),
            user_paid_total=Decimal('9000.00'),
        )

        self.orders = []
        self.payments = []
        for i in range(3):
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

    def test_batch_release_distributes_driver_payout_equally(self):
        from domain.services.payment_services import ReleasePaymentService

        BATCH_COUNT = 3
        EXPECTED_DRIVER_PER_ORDER = (Decimal('6000.00') / BATCH_COUNT).quantize(Decimal('0.01'))

        logger.info("\n--- Batch test: releasing %d payments ---", BATCH_COUNT)
        for i, payment in enumerate(self.payments):
            logger.info("Releasing payment %d/%d", i + 1, BATCH_COUNT)
            ReleasePaymentService.execute(payment.id)
            payment.refresh_from_db()
            self.assertEqual(payment.status, 'DISBURSED')
            driver_bal = self._balance(self.driver_user)
            expected_so_far = EXPECTED_DRIVER_PER_ORDER * (i + 1)
            logger.info("  Driver balance: %s (expected so far: %s)", driver_bal, expected_so_far)
            self.assertEqual(driver_bal, expected_so_far)

        self._log_wallet(self.restaurant_owner, 'After')
        self._log_wallet(self.driver_user, 'After')

        commission = Decimal('0.15')
        expected_per_order_restaurant = (Decimal('10000.00') * (Decimal('1.0') - commission)).quantize(Decimal('0.01'))
        expected_restaurant_total = expected_per_order_restaurant * BATCH_COUNT
        rest_bal = self._balance(self.restaurant_owner)
        self.assertEqual(rest_bal, expected_restaurant_total,
                         f"Restaurant owner: expected {expected_restaurant_total}, got {rest_bal}")

        driver_bal = self._balance(self.driver_user)
        self.assertEqual(driver_bal, Decimal('6000.00'),
                         f"Driver: expected 6000.00, got {driver_bal}")

        logger.info(">>> BATCH DISTRIBUTION TEST PASSED <<<")
