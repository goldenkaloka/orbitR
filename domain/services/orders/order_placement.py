import logging
import random
from decimal import Decimal

import h3
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3
from apps.geo.models import ZoneH3Index
from apps.orders.models import Order, OrderItem
from apps.restaurants.models import Restaurant
from django.db import transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import OrderConfirmed, OrderCreated
from domain.fsm.order_fsm import OrderFSM
from domain.services.pricing_service import PricingService

logger = logging.getLogger(__name__)

class PlaceOrderService:
    @staticmethod
    def execute(validated_data):
        items_data = validated_data.pop('items')
        restaurant_id = validated_data.get('restaurant')
        if isinstance(restaurant_id, Restaurant):
            restaurant = restaurant_id
        else:
            restaurant = Restaurant.objects.get(id=restaurant_id)
            
        delivery_location = validated_data.get('delivery_location')
        
        delivery_h3 = None
        if delivery_location:
            delivery_h3 = latlng_to_h3(delivery_location.y, delivery_location.x, ZONE_RESOLUTION)
            
            # --- Gate 1: OperationalZone check (Bypassed) ---
            # op_zone = ZoneH3Index.objects.filter(...)
            
            # --- Gate 2: Restaurant delivery radius check (Bypassed for limitless delivery) ---
            # We bypass the max_delivery_radius_km check so delivery can happen anywhere.
        
        # Calculate dynamic delivery and service fees
        dynamic_fee, service_fee, fee_log = PricingService.calculate_delivery_fee(restaurant, delivery_location)
        
        # Validate against client expectations
        expected_delivery = validated_data.pop('expected_delivery_fee', None)
        expected_service = validated_data.pop('expected_service_fee', None)
        
        if expected_delivery is not None and abs(Decimal(str(expected_delivery)) - dynamic_fee) > 10:
            logger.warning(f"Delivery fee mismatch: Client expected {expected_delivery}, Backend calculated {dynamic_fee}")
            
        if expected_service is not None and abs(Decimal(str(expected_service)) - service_fee) > 10:
            logger.warning(f"Service fee mismatch: Client expected {expected_service}, Backend calculated {service_fee}")
        
        if not restaurant.is_active:
            raise ValueError(f"Restaurant {restaurant.name} is currently not accepting orders.")

        items_subtotal = Decimal('0.00')
        for item_data in items_data:
            food = item_data['food']
            if not food.is_available:
                raise ValueError(f"Item {food.name} is currently unavailable.")
            quantity = item_data['quantity']
            items_subtotal += (food.price * quantity)
            
        total_amount = items_subtotal + dynamic_fee + service_fee
        otp = str(random.randint(1000, 9999))
        
        with transaction.atomic():
            order = Order.objects.create(
                items_subtotal=items_subtotal,
                delivery_fee=dynamic_fee, 
                service_fee=service_fee,
                total_amount=total_amount,
                delivery_otp=otp,
                h3_index=delivery_h3,
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
