import logging
import random
from decimal import Decimal

from apps.core.geo_utils import haversine_distance
from apps.delivery.models import DeliveryAssignment
from apps.orders.models import Order
from django.db import transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import FoodReady, OrderDelivered, OrderPicked, OrderPreparing
from domain.fsm.driver_fsm import DriverFSM
from domain.fsm.order_fsm import OrderFSM
from domain.services.driver_service import release_driver
from domain.services.permissions import check_restaurant_permission

logger = logging.getLogger(__name__)

PICKUP_GEOFENCE_RADIUS_METERS = 1000
DELIVERY_GEOFENCE_RADIUS_METERS = 1000


class StartPreparingService:
    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if not order.restaurant:
                raise ValueError("This order is not associated with a restaurant.")
            if not check_restaurant_permission(actor, order.restaurant):
                raise PermissionError("Only the restaurant owner or staff can prepare orders.")

            new_state = OrderFSM.transition(order.status, 'PREPARING')
            order.status = new_state
            order.preparing_at = timezone.now()
            order.save()

            transaction.on_commit(lambda: EventBus.publish(OrderPreparing(
                aggregate_id=order.id,
                payload={'actor_id': str(actor.id)}
            )))
            return order


class MarkReadyService:
    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if not order.restaurant:
                raise ValueError("This order is not associated with a restaurant.")
            if not check_restaurant_permission(actor, order.restaurant):
                raise PermissionError("Only the restaurant owner or staff can mark orders as ready.")

            new_state = OrderFSM.transition(order.status, 'READY')
            order.status = new_state
            order.ready_at = timezone.now()
            order.save()

            transaction.on_commit(lambda: EventBus.publish(FoodReady(
                aggregate_id=order.id,
                payload={'actor_id': str(actor.id)}
            )))
            return order


class PickupOrderService:
    @staticmethod
    def execute(order_id, actor, bypass_geofence=False):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)

            if actor.role != 'driver':
                raise PermissionError("Only drivers can perform order pickups.")

            if not getattr(order, 'driver', None) or order.driver.user != actor:
                raise PermissionError("Only the assigned driver can pickup this order.")

            driver = actor.driver_profile
            if not bypass_geofence:
                if not driver.location or not order.restaurant.geo_location:
                    raise ValueError("Location data missing for geofence check")

                dist = haversine_distance(driver.location, order.restaurant.geo_location)
                if dist > PICKUP_GEOFENCE_RADIUS_METERS:
                    raise ValueError(f"Too far from restaurant ({int(dist)}m away, {PICKUP_GEOFENCE_RADIUS_METERS}m limit)")

            order.delivery_otp = ''.join([str(random.randint(0, 9)) for _ in range(4)])

            new_state = OrderFSM.transition(order.status, 'PICKED')
            order.status = new_state
            order.picked_up_at = timezone.now()
            order.save()

            # Driver is already in DELIVERING state (set on batch accept / claim).
            # No driver state change needed here.
            assignment = order.delivery_assignment
            assignment.picked_at = timezone.now()
            assignment.save()

            transaction.on_commit(lambda: EventBus.publish(OrderPicked(
                aggregate_id=order.id,
                payload={'actor_id': str(actor.id)}
            )))
            return order


class DeliverOrderService:
    @staticmethod
    def execute(order_id, actor, otp=None, bypass_geofence=False):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if actor.role != 'driver':
                raise PermissionError("Only drivers can perform delivery completions.")

            if not getattr(order, 'driver', None) or order.driver.user != actor:
                raise PermissionError("Only the assigned driver can complete this delivery.")

            if not otp or otp != order.delivery_otp:
                raise ValueError("Invalid delivery OTP.")

            driver = actor.driver_profile
            if not bypass_geofence:
                if not driver.location or not order.delivery_location:
                    raise ValueError("Location data missing for geofence check")

                dist = haversine_distance(driver.location, order.delivery_location)
                if dist > DELIVERY_GEOFENCE_RADIUS_METERS:
                    raise ValueError(f"Too far from delivery location ({int(dist)}m away, {DELIVERY_GEOFENCE_RADIUS_METERS}m limit)")

            new_state = OrderFSM.transition(order.status, 'DELIVERED')
            order.status = new_state
            order.delivered_at = timezone.now()
            order.save()

            # If the order is part of a batch or pool batch, calculate its share of the batch's driver payout.
            if order.batch_id and order.batch.driver_payout:
                batch_order_count = order.batch.orders.count()
                order_driver_payout = Decimal(str(order.batch.driver_payout)) / batch_order_count if batch_order_count > 0 else Decimal('0')
            elif order.pool_id and order.pool.batch_id and order.pool.batch.driver_payout:
                paid_member_count = order.pool.members.filter(status='PAID').count()
                order_driver_payout = Decimal(str(order.pool.batch.driver_payout)) / paid_member_count if paid_member_count > 0 else Decimal('0')
            else:
                from domain.services.pricing_service import PricingService
                policy = PricingService.get_active_policy()
                driver_share = Decimal('0.90')
                if policy and policy.single_order_driver_share:
                    driver_share = policy.single_order_driver_share
                order_driver_payout = Decimal(str(order.delivery_fee or 0)) * driver_share

            # Return driver to AVAILABLE when no more active deliveries.
            active_orders_count = Order.objects.filter(
                driver=driver, status__in=['ASSIGNED', 'PICKED']
            ).exclude(id=order.id).count()
            if active_orders_count == 0:
                driver.status = DriverFSM.transition(driver.status, 'AVAILABLE')

            driver.total_earnings += order_driver_payout
            driver.today_earnings += order_driver_payout
            driver.current_active_orders = max(0, driver.current_active_orders - 1)
            driver.save(update_fields=[
                'status', 'total_earnings', 'today_earnings', 'current_active_orders'
            ])

            assignment = order.delivery_assignment
            assignment.delivered_at = timezone.now()
            assignment.save()

            transaction.on_commit(lambda: EventBus.publish(OrderDelivered(
                aggregate_id=order.id,
                payload={'actor_id': str(actor.id)}
            )))
            return order


class CompletePickupService:
    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().select_related('restaurant').get(id=order_id)
            if getattr(order, 'fulfillment_type', 'DELIVERY') != 'PICKUP':
                raise ValueError("This action is only available for pickup orders.")

            is_customer = order.customer_id == actor.id
            is_restaurant_user = check_restaurant_permission(actor, order.restaurant)
            if not is_customer and not is_restaurant_user:
                raise PermissionError("Only the customer or restaurant can complete this pickup.")

            if order.status != 'READY':
                raise ValueError("Pickup can only be completed after the order is ready.")

            new_state = OrderFSM.transition(order.status, 'DELIVERED')
            order.status = new_state
            order.picked_up_at = timezone.now()
            order.delivered_at = order.picked_up_at
            order.save(update_fields=['status', 'picked_up_at', 'delivered_at', 'updated_at'])

            transaction.on_commit(lambda: EventBus.publish(OrderDelivered(
                aggregate_id=order.id,
                payload={'actor_id': str(actor.id), 'fulfillment_type': 'PICKUP'}
            )))
            return order
