from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from apps.orders.models import Order
from apps.payments.models import FailedRefund, Payment
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.users.models import User
from django.conf import settings
from django.contrib.gis.geos import Point
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from domain.fsm.order_fsm import InvalidStateTransitionError, OrderFSM
from domain.services.orders.order_cancellation import CancelOrderService
from domain.services.orders.scheduled_orders import (
    PreorderDeclineService,
    PreorderSlotUnavailable,
    ReleaseScheduledOrderService,
    ensure_preorder_slot_capacity,
    prep_minutes_for,
    release_at_for,
    slot_capacity_for,
    slot_window_for,
    validate_scheduled_for,
)
from domain.services.payment_services import CompletePaymentService

RESTAURANT_POINT = Point(39.2083, -6.7924)


class ScheduledStateTests(TestCase):
    def test_pending_payment_can_be_scheduled(self):
        self.assertEqual(
            OrderFSM.transition('PENDING_PAYMENT', 'SCHEDULED'), 'SCHEDULED',
        )

    def test_scheduled_can_be_released_or_cancelled(self):
        self.assertEqual(OrderFSM.transition('SCHEDULED', 'CREATED'), 'CREATED')
        self.assertEqual(OrderFSM.transition('SCHEDULED', 'CANCELLED'), 'CANCELLED')

    def test_scheduled_cannot_skip_straight_to_preparing(self):
        with self.assertRaises(InvalidStateTransitionError):
            OrderFSM.transition('SCHEDULED', 'PREPARING')


class PreOrderTimingTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(
            username='owner_pre', phone='+255700001001',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Pre Cafe',
            location='Main Hall',
            geo_location=RESTAURANT_POINT,
            average_prep_time=25,
        )

    def test_release_is_prep_time_before_eating_time(self):
        scheduled_for = timezone.now() + timedelta(hours=3)

        self.assertEqual(
            release_at_for(self.restaurant, scheduled_for),
            scheduled_for - timedelta(minutes=25),
        )

    def test_rejects_too_soon(self):
        now = timezone.now()
        with self.assertRaisesMessage(ValueError, 'at least'):
            validate_scheduled_for(now + timedelta(minutes=10), now=now)

    def test_rejects_beyond_horizon(self):
        now = timezone.now()
        with self.assertRaisesMessage(ValueError, 'at most'):
            validate_scheduled_for(now + timedelta(days=30), now=now)

    def test_accepts_inside_the_booking_window(self):
        now = timezone.now()
        scheduled_for = now + timedelta(hours=4)

        self.assertEqual(validate_scheduled_for(scheduled_for, now=now), scheduled_for)

    def test_release_uses_the_slowest_section_prep_time(self):
        RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Grill', prep_time=90,
        )
        scheduled_for = timezone.now() + timedelta(hours=4)

        self.assertEqual(prep_minutes_for(self.restaurant), 90)
        self.assertEqual(
            release_at_for(self.restaurant, scheduled_for),
            scheduled_for - timedelta(minutes=90),
        )

    def test_rejects_a_time_whose_prep_window_already_started(self):
        now = timezone.now()
        # 45 min lead + 25 min prep = 70 min; 60 min would start prep in the
        # past and be released immediately instead of at the booked time.
        with self.assertRaisesMessage(ValueError, 'at least'):
            validate_scheduled_for(
                now + timedelta(minutes=60), now=now, restaurant=self.restaurant,
            )

        exactly = now + timedelta(minutes=70)
        self.assertEqual(
            validate_scheduled_for(exactly, now=now, restaurant=self.restaurant),
            exactly,
        )


@override_settings(MOCK_PAYMENTS=True)
class ReleaseScheduledOrderTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='cust_pre', phone='+255700001002',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='owner_pre2', phone='+255700001003',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Pre Diner',
            location='Main Hall',
            geo_location=RESTAURANT_POINT,
            average_prep_time=25,
        )
        RestaurantSection.objects.create(restaurant=self.restaurant, name='Main')

    def _make_order(self, **overrides):
        scheduled_for = overrides.pop(
            'scheduled_for', timezone.now() + timedelta(hours=2),
        )
        defaults = dict(
            customer=self.customer,
            restaurant=self.restaurant,
            status='SCHEDULED',
            is_paid=True,
            items_subtotal=Decimal('10000.00'),
            delivery_fee=Decimal('0.00'),
            service_fee=Decimal('0.00'),
            total_amount=Decimal('10000.00'),
            scheduled_for=scheduled_for,
            release_at=release_at_for(self.restaurant, scheduled_for),
        )
        defaults.update(overrides)
        return Order.objects.create(**defaults)

    def test_release_confirms_a_paid_scheduled_order(self):
        order = self._make_order()

        ReleaseScheduledOrderService.execute(str(order.id))

        order.refresh_from_db()
        self.assertEqual(order.status, 'CONFIRMED')
        self.assertIsNotNone(order.confirmed_at)

    def test_release_is_idempotent(self):
        order = self._make_order()

        ReleaseScheduledOrderService.execute(str(order.id))
        again = ReleaseScheduledOrderService.execute(str(order.id))

        self.assertEqual(again.status, 'CONFIRMED')

    def test_unpaid_scheduled_order_is_left_alone(self):
        order = self._make_order(is_paid=False)

        ReleaseScheduledOrderService.execute(str(order.id))

        order.refresh_from_db()
        self.assertEqual(order.status, 'SCHEDULED')

    def test_paid_future_preorder_parks_in_scheduled(self):
        scheduled_for = timezone.now() + timedelta(hours=3)
        order = self._make_order(
            status='PENDING_PAYMENT',
            is_paid=False,
            scheduled_for=scheduled_for,
        )
        payment = Payment.objects.create(
            payable_type='order',
            payable_id=str(order.id),
            amount=Decimal('10000.00'),
            status='INITIATED',
        )

        with transaction.atomic():
            CompletePaymentService._handle_order_payment(payment)

        order.refresh_from_db()
        self.assertTrue(order.is_paid)
        self.assertEqual(order.status, 'SCHEDULED')

    def test_paid_preorder_past_its_release_time_confirms_immediately(self):
        scheduled_for = timezone.now() + timedelta(minutes=30)
        order = self._make_order(
            status='PENDING_PAYMENT',
            is_paid=False,
            scheduled_for=scheduled_for,
        )
        order.release_at = timezone.now() - timedelta(minutes=5)
        order.save(update_fields=['release_at'])
        payment = Payment.objects.create(
            payable_type='order',
            payable_id=str(order.id),
            amount=Decimal('10000.00'),
            status='INITIATED',
        )

        with transaction.atomic():
            CompletePaymentService._handle_order_payment(payment)

        order.refresh_from_db()
        self.assertEqual(order.status, 'CONFIRMED')

    def test_sweep_releases_only_orders_whose_prep_window_started(self):
        due = self._make_order()
        due.release_at = timezone.now() - timedelta(minutes=1)
        due.save(update_fields=['release_at'])
        future = self._make_order()

        from apps.orders.tasks import release_scheduled_orders

        result = release_scheduled_orders()

        due.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual(due.status, 'CONFIRMED')
        self.assertEqual(future.status, 'SCHEDULED')
        self.assertIn('1', result)

    def test_customer_can_cancel_a_scheduled_order_without_a_fee(self):
        order = self._make_order()
        payment = Payment.objects.create(
            payable_type='order',
            payable_id=str(order.id),
            amount=Decimal('10000.00'),
            status='COLLECTED',
        )

        CancelOrderService.execute(
            order_id=str(order.id),
            actor=self.customer,
            reason='Plans changed',
        )

        order.refresh_from_db()
        self.assertEqual(order.status, 'CANCELLED')
        self.assertFalse(
            payment.transactions.filter(
                transaction_type='platform_cancel_fee',
            ).exists()
        )
        self.assertEqual(
            payment.transactions.filter(
                transaction_type='platform_to_customer_refund',
            ).count(),
            1,
        )


class PreOrderSlotCapacityTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='cust_slot', phone='+255700001004',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='owner_slot', phone='+255700001005',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Slot Diner',
            location='Main Hall',
            geo_location=RESTAURANT_POINT,
            average_prep_time=20,
        )
        # The post-save signal creates a default "Main Kitchen" section. This
        # test is about one section's throughput, so replace it rather than
        # counting two sections and doubling the expected capacity.
        self.restaurant.sections.all().delete()
        self.section = RestaurantSection.objects.create(
            restaurant=self.restaurant, name='Main', prep_time=20,
        )
        self.food = Food.objects.create(
            section=self.section, name='Lunch Plate', price=Decimal('10000.00'),
        )

    def _slot_time(self):
        # Next-day lunch service, in the restaurant's local timezone.
        tz = ZoneInfo(settings.PREORDER_TIMEZONE)
        base = (timezone.now() + timedelta(days=1)).astimezone(tz)
        return base.replace(hour=12, minute=0, second=0, microsecond=0)

    def _make_slot_order(self, scheduled_for, status='SCHEDULED'):
        return Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status=status,
            is_paid=status != 'PENDING_PAYMENT',
            items_subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
            scheduled_for=scheduled_for,
            release_at=release_at_for(self.restaurant, scheduled_for),
        )

    def test_slot_floors_the_eating_time(self):
        scheduled = datetime(2026, 10, 24, 12, 7, tzinfo=dt_timezone.utc)

        start, end = slot_window_for(scheduled)

        self.assertEqual(start, datetime(2026, 10, 24, 12, 0, tzinfo=dt_timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 24, 12, 15, tzinfo=dt_timezone.utc))

    def test_capacity_is_derived_from_section_throughput(self):
        # 5 concurrent orders, 20 min prep, 15 min slot -> ceil(75/20) = 4
        self.assertEqual(slot_capacity_for(self.restaurant), 4)

    def test_explicit_restaurant_capacity_wins(self):
        self.restaurant.preorder_slot_capacity = 2
        self.restaurant.save(update_fields=['preorder_slot_capacity'])

        self.assertEqual(slot_capacity_for(self.restaurant), 2)

    def test_zero_capacity_disables_preorders(self):
        self.restaurant.preorder_slot_capacity = 0
        self.restaurant.save(update_fields=['preorder_slot_capacity'])

        with self.assertRaisesMessage(PreorderSlotUnavailable, 'not accepting'):
            ensure_preorder_slot_capacity(self.restaurant, self._slot_time())

    def test_full_slot_is_refused(self):
        self.restaurant.preorder_slot_capacity = 2
        self.restaurant.save(update_fields=['preorder_slot_capacity'])
        scheduled_for = self._slot_time()
        self._make_slot_order(scheduled_for)
        self._make_slot_order(scheduled_for)

        with self.assertRaisesMessage(PreorderSlotUnavailable, 'fully booked'):
            ensure_preorder_slot_capacity(self.restaurant, scheduled_for)

    def test_another_slot_has_its_own_capacity(self):
        self.restaurant.preorder_slot_capacity = 1
        self.restaurant.save(update_fields=['preorder_slot_capacity'])
        scheduled_for = self._slot_time()
        next_slot = scheduled_for + timedelta(minutes=15)
        self._make_slot_order(scheduled_for)

        # The next slot is empty, so a booking there is allowed.
        ensure_preorder_slot_capacity(self.restaurant, next_slot)

        self._make_slot_order(next_slot)
        with self.assertRaises(PreorderSlotUnavailable):
            ensure_preorder_slot_capacity(self.restaurant, next_slot)

    def test_cancelled_orders_free_capacity(self):
        self.restaurant.preorder_slot_capacity = 1
        self.restaurant.save(update_fields=['preorder_slot_capacity'])
        scheduled_for = self._slot_time()
        self._make_slot_order(scheduled_for, status='CANCELLED')

        ensure_preorder_slot_capacity(self.restaurant, scheduled_for)

    def test_placement_reserves_the_slot_and_rejects_overflow(self):
        from domain.services.orders.order_placement import PlaceOrderService

        self.restaurant.preorder_slot_capacity = 1
        self.restaurant.save(update_fields=['preorder_slot_capacity'])
        scheduled_for = self._slot_time()

        order = PlaceOrderService.execute({
            'customer': self.customer,
            'restaurant': self.restaurant,
            'items': [{'food': self.food, 'quantity': 1}],
            'scheduled_for': scheduled_for,
        })
        self.assertEqual(order.status, 'PENDING_PAYMENT')
        self.assertEqual(order.scheduled_for, scheduled_for)

        with self.assertRaises(PreorderSlotUnavailable):
            PlaceOrderService.execute({
                'customer': self.customer,
                'restaurant': self.restaurant,
                'items': [{'food': self.food, 'quantity': 1}],
                'scheduled_for': scheduled_for,
            })


class _RefusingGateway:
    def refund(self, transaction_id=None, amount=None, **kwargs):
        return {'success': False, 'message': 'Insufficient provider balance'}


class _AcceptingGateway:
    def refund(self, transaction_id=None, amount=None, **kwargs):
        return {'success': True, 'refund_id': f"RETRY_{transaction_id}"}


@override_settings(MOCK_PAYMENTS=True)
class PreorderDeclineTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='cust_decline', phone='+255700001010',
            password='password123', role='customer',
        )
        self.owner = User.objects.create_user(
            username='owner_decline', phone='+255700001011',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=self.owner,
            name='Decline Diner',
            location='Main Hall',
            geo_location=RESTAURANT_POINT,
            average_prep_time=25,
            preorder_slot_capacity=1,
        )
        RestaurantSection.objects.create(restaurant=self.restaurant, name='Main')
        tz = ZoneInfo(settings.PREORDER_TIMEZONE)
        self.slot = (timezone.now() + timedelta(days=1)).astimezone(tz).replace(
            hour=12, minute=0, second=0, microsecond=0,
        )

    def _make_order(self, status='SCHEDULED', scheduled_for='slot', payment_status='COLLECTED'):
        scheduled_for = self.slot if scheduled_for == 'slot' else scheduled_for
        order = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status=status,
            is_paid=payment_status == 'COLLECTED',
            items_subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
            scheduled_for=scheduled_for,
            release_at=(
                release_at_for(self.restaurant, scheduled_for)
                if scheduled_for else None
            ),
        )
        payment = Payment.objects.create(
            payable_type='order',
            payable_id=str(order.id),
            amount=Decimal('10000.00'),
            status=payment_status,
        )
        return order, payment

    def test_decline_cancels_and_refunds_in_full(self):
        order, payment = self._make_order()

        PreorderDeclineService.execute(str(order.id), self.owner)

        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.status, 'CANCELLED')
        self.assertEqual(payment.status, 'REFUNDED')
        self.assertEqual(
            payment.transactions.filter(
                transaction_type='platform_to_customer_refund',
            ).count(),
            1,
        )
        self.assertFalse(
            payment.transactions.filter(
                transaction_type='platform_cancel_fee',
            ).exists()
        )

    def test_decline_is_recorded_as_a_vendor_fault(self):
        order, _ = self._make_order()

        PreorderDeclineService.execute(str(order.id), self.owner)

        order.refresh_from_db()
        self.assertEqual(order.cancelled_by, 'VENDOR')
        self.assertEqual(order.cancellation_fault, 'VENDOR')
        self.assertEqual(order.cancellation_category, 'vendor_rejected')
        self.assertTrue(order.affects_restaurant_reputation)

    def test_decline_frees_the_slot(self):
        order, _ = self._make_order()

        with self.assertRaisesMessage(PreorderSlotUnavailable, 'fully booked'):
            ensure_preorder_slot_capacity(self.restaurant, self.slot)

        PreorderDeclineService.execute(str(order.id), self.owner)

        ensure_preorder_slot_capacity(self.restaurant, self.slot)

    def test_customer_cannot_decline(self):
        order, _ = self._make_order()

        with self.assertRaises(PermissionError):
            PreorderDeclineService.execute(str(order.id), self.customer)

        order.refresh_from_db()
        self.assertEqual(order.status, 'SCHEDULED')

    def test_another_restaurants_owner_cannot_decline(self):
        intruder = User.objects.create_user(
            username='other_vendor', phone='+255700001012',
            password='password123', role='vendor',
        )
        order, _ = self._make_order()

        with self.assertRaises(PermissionError):
            PreorderDeclineService.execute(str(order.id), intruder)

        order.refresh_from_db()
        self.assertEqual(order.status, 'SCHEDULED')

    def test_only_preorders_can_be_declined_this_way(self):
        order, _ = self._make_order(status='CONFIRMED', scheduled_for=None)

        with self.assertRaisesMessage(ValueError, 'not a pre-order'):
            PreorderDeclineService.execute(str(order.id), self.owner)

        order.refresh_from_db()
        self.assertEqual(order.status, 'CONFIRMED')

    def test_cannot_decline_once_the_food_is_ready(self):
        order, _ = self._make_order(status='READY')

        with self.assertRaisesMessage(ValueError, 'Cannot decline'):
            PreorderDeclineService.execute(str(order.id), self.owner)

        order.refresh_from_db()
        self.assertEqual(order.status, 'READY')

    def test_cannot_decline_once_cooking_has_started(self):
        order, _ = self._make_order(status='PREPARING')

        with self.assertRaisesMessage(ValueError, 'Cannot decline'):
            PreorderDeclineService.execute(str(order.id), self.owner)

        order.refresh_from_db()
        self.assertEqual(order.status, 'PREPARING')

    def test_refused_refund_is_queued_and_then_retried(self):
        from apps.payments.tasks import retry_failed_refunds

        order, payment = self._make_order()

        with patch(
            'domain.services.payment_services.get_payment_gateway',
            return_value=_RefusingGateway(),
        ):
            PreorderDeclineService.execute(str(order.id), self.owner)

        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.status, 'CANCELLED')
        self.assertEqual(payment.status, 'COLLECTED')

        record = FailedRefund.objects.get(payment=payment)
        self.assertEqual(record.amount, Decimal('10000.00'))

        with patch(
            'domain.services.payment_services.get_payment_gateway',
            return_value=_AcceptingGateway(),
        ):
            result = retry_failed_refunds()

        payment.refresh_from_db()
        self.assertEqual(payment.status, 'REFUNDED')
        self.assertFalse(FailedRefund.objects.filter(payment=payment).exists())
        self.assertIn('1 completed', result)


@override_settings(MOCK_PAYMENTS=True)
class LatePaymentAfterCancellationTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='cust_late', phone='+255700001020',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='owner_late', phone='+255700001021',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Late Diner',
            location='Main Hall',
            geo_location=RESTAURANT_POINT,
            average_prep_time=25,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='CANCELLED',
            is_paid=False,
            items_subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
            cancelled_at=timezone.now(),
            cancelled_by='SYSTEM',
            cancellation_category='payment_timeout',
            cancellation_reason='Payment timeout before collection',
        )
        self.payment = Payment.objects.create(
            payable_type='order',
            payable_id=str(self.order.id),
            amount=Decimal('10000.00'),
            status='INITIATED',
        )

    def test_late_success_is_refunded_without_resurrecting_the_order(self):
        with self.captureOnCommitCallbacks(execute=True):
            CompletePaymentService.execute(
                payment_id=self.payment.id,
                provider_reference='LATE_SUCCESS',
                success=True,
            )

        self.order.refresh_from_db()
        self.payment.refresh_from_db()

        # The order stays cancelled; the money is recorded and returned.
        self.assertEqual(self.order.status, 'CANCELLED')
        self.assertFalse(self.order.is_paid)
        self.assertEqual(self.payment.status, 'REFUNDED')
        self.assertTrue(
            self.payment.transactions.filter(
                transaction_type='customer_to_platform',
            ).exists(),
        )
        self.assertTrue(
            self.payment.transactions.filter(
                transaction_type='platform_to_customer_refund',
            ).exists(),
        )


@override_settings(MOCK_PAYMENTS=True)
class PreorderBookingNotificationTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='cust_booked', phone='+255700001022',
            password='password123', role='customer',
        )
        owner = User.objects.create_user(
            username='owner_booked', phone='+255700001023',
            password='password123', role='vendor',
        )
        self.restaurant = Restaurant.objects.create(
            owner=owner,
            name='Booking Diner',
            location='Main Hall',
            geo_location=RESTAURANT_POINT,
            average_prep_time=25,
        )

    def test_paid_preorder_confirms_the_booking_to_customer_and_vendor(self):
        scheduled_for = timezone.now() + timedelta(hours=3)
        order = Order.objects.create(
            customer=self.customer,
            restaurant=self.restaurant,
            status='PENDING_PAYMENT',
            is_paid=False,
            items_subtotal=Decimal('10000.00'),
            total_amount=Decimal('10000.00'),
            scheduled_for=scheduled_for,
            release_at=release_at_for(self.restaurant, scheduled_for),
        )
        payment = Payment.objects.create(
            payable_type='order',
            payable_id=str(order.id),
            amount=Decimal('10000.00'),
            status='INITIATED',
        )

        with (
            patch(
                'infrastructure.notifications.service.NotificationService.send_push',
            ) as push,
            self.captureOnCommitCallbacks(execute=True),
        ):
            CompletePaymentService.execute(
                payment_id=payment.id,
                provider_reference='PREORDER_PAID',
                success=True,
            )

        order.refresh_from_db()
        self.assertEqual(order.status, 'SCHEDULED')

        pushed_types = [
            call.kwargs.get('data', {}).get('type')
            for call in push.call_args_list
        ]
        self.assertIn('preorder_booked', pushed_types)
        self.assertIn('vendor_preorder_booked', pushed_types)


class ScheduledForValidationTests(TestCase):
    def test_null_scheduled_for_is_treated_as_now(self):
        from api.serializers.core import OrderSerializer

        self.assertIsNone(OrderSerializer().validate_scheduled_for(None))
