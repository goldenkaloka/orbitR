import logging
import random
from decimal import Decimal
from django.db import transaction
from apps.orders.models import Order, OrderItem
from apps.restaurants.models import Restaurant, DeliveryZone
from domain.events.bus import EventBus
from domain.events.types import OrderCreated, OrderConfirmed
from domain.services.pricing_service import PricingService
from domain.fsm.order_fsm import OrderFSM
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3

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
        
        # Calculate dynamic delivery and service fees
        dynamic_fee, service_fee, fee_log = PricingService.calculate_delivery_fee(restaurant, delivery_location)
        
        # Validate against client expectations
        expected_delivery = validated_data.pop('expected_delivery_fee', None)
        expected_service = validated_data.pop('expected_service_fee', None)
        
        if expected_delivery is not None and abs(Decimal(str(expected_delivery)) - dynamic_fee) > 10:
            logger.warning(f"Delivery fee mismatch: Client expected {expected_delivery}, Backend calculated {dynamic_fee}")
            
        if expected_service is not None and abs(Decimal(str(expected_service)) - service_fee) > 10:
            logger.warning(f"Service fee mismatch: Client expected {expected_service}, Backend calculated {service_fee}")
        
        zone = None
        delivery_h3 = None
        if delivery_location:
            delivery_h3 = latlng_to_h3(delivery_location.y, delivery_location.x, ZONE_RESOLUTION)
            zone = DeliveryZone.objects.filter(
                restaurant=restaurant, 
                is_active=True, 
                h3_index=delivery_h3
            ).order_by('-priority').first()
        
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
                zone=zone,
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
                    price=item_data['food'].price * item_data['quantity']
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
            order.save()
            
            # CONSISTENCY: Added on_commit
            transaction.on_commit(lambda: EventBus.publish(OrderConfirmed(
                aggregate_id=order.id, 
                payload={'actor_id': str(actor.id)}
            )))
            return order
