import logging
import random
from decimal import Decimal

import h3
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3
from apps.geo.models import ZoneH3Index
from apps.orders.models import DropWindow, Order, OrderItem
from apps.restaurants.models import Restaurant
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import OrderConfirmed, OrderCreated
from domain.fsm.order_fsm import OrderFSM
from domain.services.orders.scheduled_orders import (
    ensure_preorder_slot_capacity,
    release_at_for,
    validate_scheduled_for,
)
from domain.services.orders.drop_service import (
    DropUnavailable,
    validate_drop_booking,
)
from domain.services.pricing_service import PricingService

logger = logging.getLogger(__name__)


def _distance_km(point_a, point_b):
    if not point_a or not point_b:
        return None
    return h3.great_circle_distance((point_a.y, point_a.x), (point_b.y, point_b.x))

class PlaceOrderService:
    @staticmethod
    def execute(validated_data):
        items_data = validated_data.pop('items')
        restaurant_id = validated_data.get('restaurant')
        if isinstance(restaurant_id, Restaurant):
            restaurant = restaurant_id
        else:
            restaurant = Restaurant.objects.get(id=restaurant_id)
            
        fulfillment_type = str(
            validated_data.get('fulfillment_type') or Order.FULFILLMENT_DELIVERY
        ).upper()
        if fulfillment_type not in {Order.FULFILLMENT_DELIVERY, Order.FULFILLMENT_PICKUP}:
            raise ValueError("Invalid fulfillment type.")

        # ── Scheduled drop ────────────────────────────────────────────────────
        # A drop order is delivered to a shared point for a recurring window.
        # The point replaces the customer's address, the window's flat fee
        # replaces distance pricing, and the window's start is the eating time.
        drop_window = validated_data.get('drop_window')
        drop_start = None
        if drop_window is not None:
            if not isinstance(drop_window, DropWindow):
                try:
                    drop_window = DropWindow.objects.select_related('drop_point').get(
                        id=drop_window,
                    )
                except (DropWindow.DoesNotExist, ValueError, TypeError):
                    raise ValueError("This drop window does not exist.")
            try:
                drop_start = validate_drop_booking(
                    drop_window, validated_data.get('scheduled_for'),
                    restaurant=restaurant,
                )
            except DropUnavailable as exc:
                raise ValueError(str(exc))

            point = drop_window.drop_point
            fulfillment_type = Order.FULFILLMENT_DELIVERY
            validated_data['fulfillment_type'] = fulfillment_type
            validated_data['drop_window'] = drop_window
            validated_data['drop_point'] = point
            validated_data['scheduled_for'] = drop_start
            validated_data['delivery_location'] = point.geo_location
            validated_data['delivery_address'] = point.name
        is_drop = drop_start is not None

        delivery_location = validated_data.get('delivery_location')
        is_pickup = fulfillment_type == Order.FULFILLMENT_PICKUP
        if is_pickup:
            if not delivery_location:
                raise ValueError("Location is required to verify pickup eligibility.")
            if not restaurant.geo_location:
                raise ValueError("Restaurant location is not configured for pickup eligibility.")
            pickup_distance = _distance_km(restaurant.geo_location, delivery_location)
            max_pickup_distance = Decimal(str(getattr(settings, 'LOCAL_PICKUP_MAX_DISTANCE_KM', 0.5)))
            if pickup_distance is None or Decimal(str(pickup_distance)) > max_pickup_distance:
                raise ValueError(f"Pickup is only available within {max_pickup_distance} km of this restaurant.")
        
        delivery_h3 = None
        if delivery_location:
            delivery_h3 = latlng_to_h3(delivery_location.y, delivery_location.x, ZONE_RESOLUTION)
            
            # --- Gate 1: OperationalZone check (Bypassed) ---
            # op_zone = ZoneH3Index.objects.filter(...)
            
            # --- Gate 2: Restaurant delivery radius check (Bypassed for limitless delivery) ---
            # We bypass the max_delivery_radius_km check so delivery can happen anywhere.
        
        # Calculate dynamic delivery and service fees
        if is_drop:
            # One trip carries many orders, so the fee is the window's flat
            # price. Distance maths would charge each customer for the whole
            # route and destroy the economics of a shared drop.
            dynamic_fee = Decimal(str(drop_window.delivery_fee))
            policy = PricingService.get_active_policy()
            service_fee = (
                Decimal(str(policy.service_fee)) if policy else Decimal('300.00')
            )
            fee_log = None
        elif is_pickup:
            _, service_fee, fee_log = PricingService.calculate_delivery_fee(
                restaurant,
                delivery_location,
                skip_log=True,
            )
            dynamic_fee = Decimal('0.00')
            fee_log = None
        else:
            dynamic_fee, service_fee, fee_log = PricingService.calculate_delivery_fee(restaurant, delivery_location)
        
        # Validate against client expectations
        expected_delivery = validated_data.pop('expected_delivery_fee', None)
        expected_service = validated_data.pop('expected_service_fee', None)
        
        if expected_delivery is not None and abs(Decimal(str(expected_delivery)) - dynamic_fee) > 10:
            logger.warning(f"Delivery fee mismatch: Client expected {expected_delivery}, Backend calculated {dynamic_fee}")
            raise ValueError("Delivery fee changed. Please refresh the quote before placing the order.")
            
        if expected_service is not None and abs(Decimal(str(expected_service)) - service_fee) > 10:
            logger.warning(f"Service fee mismatch: Client expected {expected_service}, Backend calculated {service_fee}")
            raise ValueError("Service fee changed. Please refresh the quote before placing the order.")
        
        if not restaurant.is_active:
            raise ValueError(f"Restaurant {restaurant.name} is currently not accepting orders.")

        items_subtotal = Decimal('0.00')
        for item_data in items_data:
            food = item_data['food']
            if not food.is_available:
                raise ValueError(f"Item {food.name} is currently unavailable.")
            quantity = item_data['quantity']
            items_subtotal += (food.price * quantity)
            
        scheduled_for = validated_data.get('scheduled_for')
        if is_drop:
            # The window's cut-off already enforces the lead time; the kitchen
            # still needs a release moment so the food is hot at hand-over.
            validated_data['release_at'] = release_at_for(restaurant, drop_start)
        elif scheduled_for:
            validate_scheduled_for(scheduled_for, restaurant=restaurant)
            validated_data['release_at'] = release_at_for(restaurant, scheduled_for)
        else:
            validated_data.pop('release_at', None)

        total_amount = items_subtotal + dynamic_fee + service_fee
        otp = str(random.randint(1000, 9999))
        
        with transaction.atomic():
            if is_drop:
                # Serialise bookings per drop window so two customers cannot
                # both take the last seat. The row lock is the same mechanism
                # the pre-order path uses on the restaurant.
                DropWindow.objects.select_for_update().get(id=drop_window.id)
                try:
                    validate_drop_booking(drop_window, drop_start, restaurant=restaurant)
                except DropUnavailable as exc:
                    raise ValueError(str(exc))
            elif scheduled_for:
                # Serialise pre-order bookings per restaurant so two customers
                # cannot both take the same last slot.
                locked_restaurant = Restaurant.objects.select_for_update().get(
                    id=restaurant.id,
                )
                ensure_preorder_slot_capacity(locked_restaurant, scheduled_for)

            order = Order.objects.create(
                items_subtotal=items_subtotal,
                delivery_fee=dynamic_fee, 
                service_fee=service_fee,
                total_amount=total_amount,
                delivery_otp=otp,
                h3_index=delivery_h3,
                confirmation_status=(
                    Order.CONFIRMATION_PENDING
                    if (
                        restaurant.source_type == 'PLATFORM_MANAGED'
                        or restaurant.settlement_mode == 'PLATFORM_PREPAID'
                    )
                    else Order.CONFIRMATION_CONFIRMED
                ),
                **validated_data
            )
            
            if fee_log:
                fee_log.order = order
                fee_log.save()
            
            order_items = [
                OrderItem(
                    order=order,
                    food=item_data['food'],
                    quantity=item_data['quantity'],
                    price=item_data['food'].price * item_data['quantity'],
                    section=item_data['food'].section
                ) for item_data in items_data
            ]
            OrderItem.objects.bulk_create(order_items)
            
            # CONSISTENCY: Always use on_commit for events
            transaction.on_commit(lambda: EventBus.publish(OrderCreated(
                aggregate_id=order.id,
                payload={'customer_id': str(order.customer.id)}
            )))
        
        return order


class ConfirmOrderService:
    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)

            # Idempotency guard: skip if already confirmed
            if order.status == 'CONFIRMED':
                return order

            if not order.is_paid:
                raise ValueError("Cannot confirm an unpaid order")

            new_state = OrderFSM.transition(order.status, 'CONFIRMED')
            order.status = new_state
            order.confirmed_at = timezone.now()
            order.save()
            
            # CONSISTENCY: Added on_commit
            transaction.on_commit(lambda: EventBus.publish(OrderConfirmed(
                aggregate_id=order.id, 
                payload={'actor_id': str(actor.id)}
            )))
            return order
