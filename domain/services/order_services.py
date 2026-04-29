from apps.orders.models import Order
from apps.delivery.models import Driver, DeliveryAssignment
from domain.fsm.order_fsm import OrderFSM, InvalidStateTransitionError
from domain.fsm.driver_fsm import DriverFSM
from domain.events.bus import EventBus
from domain.events.types import (
    OrderConfirmed, DriverAssigned, OrderDelivered, 
    FoodReady, OrderPicked, OrderPreparing, OrderCancelled
)
from apps.events.utils import broadcast_order_status

from django.db import transaction

class PlaceOrderService:
    @staticmethod
    def execute(validated_data):
        from apps.orders.models import Order, OrderItem
        from domain.events.bus import EventBus
        from domain.events.types import OrderCreated
        import random
        from decimal import Decimal
        from django.db import transaction

        items_data = validated_data.pop('items')
        otp = str(random.randint(1000, 9999))
        
        with transaction.atomic():
            # 1. Create Order with synced delivery fee and OTP
            order = Order.objects.create(
                items_subtotal=Decimal('0.00'),
                delivery_fee=Decimal('500.00'), 
                total_amount=Decimal('0.00'),
                delivery_otp=otp,
                **validated_data
            )
            
            # 2. Prepare items and calculate subtotal
            items_subtotal = Decimal('0.00')
            order_items = []
            for item_data in items_data:
                food = item_data['food']
                quantity = item_data['quantity']
                item_price = food.price * quantity
                
                order_items.append(OrderItem(
                    order=order,
                    food=food,
                    quantity=quantity,
                    price=item_price
                ))
                items_subtotal += item_price
            
            # 3. Bulk create items and finalize totals
            OrderItem.objects.bulk_create(order_items)
            order.items_subtotal = items_subtotal
            order.total_amount = items_subtotal + order.delivery_fee
            order.save()
            
        # 4. Trigger Event
        EventBus.publish(OrderCreated(
            aggregate_id=order.id,
            payload={'customer_id': str(order.customer.id)}
        ))
        
        return order

class ConfirmOrderService:
    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            # Only customer can confirm (usually after payment) or system
            new_state = OrderFSM.transition(order.status, 'CONFIRMED')
            order.status = new_state
            order.save()
            
            EventBus.publish(OrderConfirmed(aggregate_id=order.id, payload={'actor_id': str(actor.id)}))
            return order

class StartPreparingService:
    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if not order.restaurant:
                raise ValueError("This order is not associated with a restaurant.")
            if order.restaurant.owner != actor:
                raise PermissionError("Only the restaurant owner can start preparing orders.")
            
            new_state = OrderFSM.transition(order.status, 'PREPARING')
            order.status = new_state
            order.save()
            
            EventBus.publish(OrderPreparing(aggregate_id=order.id, payload={'actor_id': str(actor.id)}))
            return order

class CancelOrderService:
    @staticmethod
    def execute(order_id, actor, reason='No reason provided'):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            
            new_state = OrderFSM.transition(order.status, 'CANCELLED')
            order.status = new_state
            order.save()
            
            EventBus.publish(OrderCancelled(aggregate_id=order.id, payload={
                'actor_id': str(actor.id),
                'reason': reason
            }))
            return order

class MarkReadyService:
    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if not order.restaurant:
                raise ValueError("This order is not associated with a restaurant.")
            if order.restaurant.owner != actor:
                raise PermissionError("Only the restaurant owner can mark orders as ready.")
            
            new_state = OrderFSM.transition(order.status, 'READY')
            order.status = new_state
            from django.utils import timezone
            order.ready_at = timezone.now()
            order.save()
            EventBus.publish(FoodReady(aggregate_id=order.id, payload={'actor_id': str(actor.id)}))
            return order

class AssignDriverService:
    @staticmethod
    def execute(order_id, driver_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if order.driver:
                raise ValueError("This order already has a driver assigned.")
            
            driver = Driver.objects.select_for_update().get(id=driver_id)
            
            # Transition Order
            new_state = OrderFSM.transition(order.status, 'ASSIGNED')
            order.status = new_state
            order.driver = driver
            order.save()
            
            # Transition Driver
            driver.status = DriverFSM.transition(driver.status, 'ASSIGNED')
            driver.save(update_fields=['status'])
            
            DeliveryAssignment.objects.get_or_create(order=order, defaults={'driver': driver})
            EventBus.publish(DriverAssigned(aggregate_id=order.id, payload={'driver_id': str(driver.id), 'actor_id': str(actor.id)}))
            return order

class PickupOrderService:
    @staticmethod
    def execute(order_id, actor, bypass_geofence=False):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if not getattr(order, 'driver', None) or order.driver.user != actor:
                raise PermissionError("Only the assigned driver can pickup this order.")
            
            driver = actor.driver_profile
            if not bypass_geofence:
                if not driver.location or not order.restaurant.geo_location:
                    raise ValueError("Location data missing for geofence check")
                if driver.location.distance(order.restaurant.geo_location) > 100:
                    raise ValueError("You are too far from the restaurant to pickup")
            
            new_state = OrderFSM.transition(order.status, 'PICKED')
            order.status = new_state
            order.save()
            
            # Transition Driver to DELIVERING
            driver.status = DriverFSM.transition(driver.status, 'DELIVERING')
            driver.save(update_fields=['status'])
            
            assignment = order.delivery_assignment
            from django.utils import timezone
            assignment.picked_at = timezone.now()
            assignment.save()

            EventBus.publish(OrderPicked(aggregate_id=order.id, payload={'actor_id': str(actor.id)}))
            return order

class DeliverOrderService:
    @staticmethod
    def execute(order_id, actor, otp=None, bypass_geofence=False):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if not getattr(order, 'driver', None) or order.driver.user != actor:
                raise PermissionError("Only the assigned driver can complete this delivery.")
            
            if not otp or otp != order.delivery_otp:
                raise ValueError("Invalid delivery OTP. Please ask the customer for their 4-digit PIN.")

            driver = actor.driver_profile
            if not bypass_geofence:
                if not driver.location or not order.delivery_location:
                    raise ValueError("Location data missing for geofence check")
                if driver.location.distance(order.delivery_location) > 100:
                    raise ValueError("You are too far from the delivery location to mark as delivered")
            
            new_state = OrderFSM.transition(order.status, 'DELIVERED')
            order.status = new_state
            order.save()
            
            # Transition Driver back to AVAILABLE
            driver.status = DriverFSM.transition(driver.status, 'AVAILABLE')
            driver.save(update_fields=['status'])
            
            assignment = order.delivery_assignment
            from django.utils import timezone
            assignment.delivered_at = timezone.now()
            assignment.save()

            EventBus.publish(OrderDelivered(aggregate_id=order.id, payload={'actor_id': str(actor.id)}))
            return order

class DispatchService:
    @staticmethod
    def execute(order_id, is_retry=False):
        """
        The core of the Automated Dispatch Logic.
        Finds the closest available driver and assigns them to the order.
        """
        from infrastructure.matching.strategies import PostgisRadiusStrategy
        from django.contrib.auth import get_user_model
        from domain.events.types import DispatchFailed
        User = get_user_model()
        
        # System user for assignment audit trail
        system_user, _ = User.objects.get_or_create(username='system_dispatcher')

        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            
            if order.driver:
                return order # Already assigned
            
            # 1. Get strategy (PostGIS preferred)
            strategy = PostgisRadiusStrategy()
            
            # 2. Find best driver based on restaurant location
            best_driver = strategy.find_best_driver(
                restaurant_lat=order.restaurant.latitude,
                restaurant_lng=order.restaurant.longitude,
                max_radius_km=15
            )
            
            if best_driver:
                # 3. Assign the driver
                AssignDriverService.execute(
                    order_id=order.id, 
                    driver_id=best_driver.id, 
                    actor=system_user
                )
                return order
            else:
                # No driver found
                if not is_retry:
                    # 4. Schedule first retry if this is the initial attempt
                    from domain.services.dispatch_tasks import retry_dispatch_task
                    transaction.on_commit(lambda: retry_dispatch_task.delay(str(order.id)))
                
                # 5. Publish failure event (for alerts/analytics)
                EventBus.publish(DispatchFailed(
                    aggregate_id=order.id,
                    payload={'order_id': str(order.id), 'attempt_retry': not is_retry}
                ))
                
                import logging
                logging.getLogger(__name__).warning(f"No available drivers found for Order {order_id}.")
                return order
