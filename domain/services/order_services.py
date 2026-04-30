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

        from domain.services.pricing_service import PricingService
        from apps.restaurants.models import Restaurant

        items_data = validated_data.pop('items')
        restaurant_id = validated_data.get('restaurant')
        if isinstance(restaurant_id, Restaurant):
            restaurant = restaurant_id
        else:
            restaurant = Restaurant.objects.get(id=restaurant_id)
            
        delivery_location = validated_data.get('delivery_location')
        
        # Calculate dynamic delivery fee
        dynamic_fee = PricingService.calculate_delivery_fee(restaurant, delivery_location)
        
        # Tag Zone for Batching
        from apps.restaurants.models import DeliveryZone
        zone = None
        if delivery_location:
            zone = DeliveryZone.objects.filter(
                restaurant=restaurant, 
                is_active=True, 
                boundary__contains=delivery_location
            ).order_by('-priority').first()
        
        otp = str(random.randint(1000, 9999))
        
        with transaction.atomic():
            # 1. Create Order with dynamic delivery fee, OTP, and Zone
            order = Order.objects.create(
                items_subtotal=Decimal('0.00'),
                delivery_fee=dynamic_fee, 
                total_amount=Decimal('0.00'),
                delivery_otp=otp,
                zone=zone,
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

class PoolOrderService:
    @staticmethod
    def create_pool(initiator, restaurant_id, delivery_address, delivery_location):
        from apps.orders.models import OrderPool, PoolMember
        from apps.restaurants.models import Restaurant
        import random
        import string
        from django.utils import timezone
        from datetime import timedelta

        restaurant = Restaurant.objects.get(id=restaurant_id)
        invite_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        
        with transaction.atomic():
            pool = OrderPool.objects.create(
                initiator=initiator,
                restaurant=restaurant,
                invite_code=invite_code,
                delivery_address=delivery_address,
                delivery_location=delivery_location,
                expires_at=timezone.now() + timedelta(hours=2)
            )
            # Add initiator as first member
            PoolMember.objects.create(pool=pool, user=initiator)
            
        return pool

    @staticmethod
    def join_pool(user, invite_code):
        from apps.orders.models import OrderPool, PoolMember
        pool = OrderPool.objects.get(invite_code=invite_code, status='OPEN')
        member, created = PoolMember.objects.get_or_create(pool=pool, user=user)
        return pool

    @staticmethod
    def add_item(user, pool_id, food_id, quantity):
        from apps.orders.models import OrderPool, PoolMember, PoolItem
        from apps.products.models import Food
        
        with transaction.atomic():
            pool = OrderPool.objects.get(id=pool_id, status='OPEN')
            member = PoolMember.objects.get(pool=pool, user=user)
            food = Food.objects.get(id=food_id)
            
            if food.restaurant != pool.restaurant:
                raise ValueError("Food must be from the same restaurant as the pool.")

            PoolItem.objects.create(
                member=member,
                food=food,
                quantity=quantity,
                price_at_addition=food.price
            )
            
            # Update member subtotal
            member.items_subtotal += (food.price * quantity)
            member.save()
            return pool

    @staticmethod
    def lock_pool(pool_id):
        """
        Calculate final fees and split the delivery cost among members.
        """
        from apps.orders.models import OrderPool
        from domain.services.pricing_service import PricingService
        
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)
            pool.status = 'LOCKED'
            
            # Calculate total delivery fee for the location
            total_fee = PricingService.calculate_delivery_fee(pool.restaurant, pool.delivery_location)
            pool.total_delivery_fee = total_fee
            
            # Split fee among members
            members = pool.members.all()
            if members.exists():
                share = total_fee / members.count()
                for member in members:
                    member.delivery_fee_share = share
                    member.save()
            
            pool.save()
            return pool

    @staticmethod
    def pay_share(user, pool_id):
        """
        Process an individual member's payment from their wallet.
        """
        from apps.orders.models import OrderPool, PoolMember
        from apps.wallets.models import Wallet, Transaction
        
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)
            if pool.status != 'LOCKED':
                raise ValueError("Pool must be locked for payment.")
            
            member = PoolMember.objects.select_for_update().get(pool=pool, user=user)
            if member.status == 'PAID':
                return pool
            
            total_owed = member.items_subtotal + member.delivery_fee_share
            
            wallet = Wallet.objects.select_for_update().get(user=user)
            if wallet.balance < total_owed:
                raise ValueError(f"Insufficient funds. Owed: {total_owed}, Balance: {wallet.balance}")
            
            # Debit Wallet
            wallet.balance -= total_owed
            wallet.save()
            
            Transaction.objects.create(
                wallet=wallet,
                amount=total_owed,
                tx_type='DEBIT',
                description=f"Payment for Pool Order {pool.invite_code}",
                pool=pool
            )
            
            member.status = 'PAID'
            member.save()
            
            # Check if everyone has paid
            all_paid = not pool.members.exclude(status='PAID').exists()
            if all_paid:
                # Use a callback or direct call to finalize
                PoolOrderService.finalize_pool(pool.id)
            
            return pool

    @staticmethod
    def finalize_pool(pool_id):
        """
        Convert the paid pool into a single Order for the vendor/rider.
        """
        from apps.orders.models import OrderPool, Order, OrderItem
        from domain.events.bus import EventBus
        from domain.events.types import OrderCreated
        
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)
            
            from apps.restaurants.models import DeliveryZone
            zone = DeliveryZone.objects.filter(
                restaurant=pool.restaurant, 
                is_active=True, 
                boundary__contains=pool.delivery_location
            ).order_by('-priority').first()
            
            # 1. Create a single master Order
            master_order = Order.objects.create(
                customer=pool.initiator, # Initiator is the primary contact
                restaurant=pool.restaurant,
                delivery_address=pool.delivery_address,
                delivery_location=pool.delivery_location,
                delivery_fee=pool.total_delivery_fee,
                pool=pool,
                zone=zone,
                status='CREATED'
            )
            
            # 2. Transfer all pool items to order items
            items_subtotal = Decimal('0.00')
            for member in pool.members.all():
                for item in member.items.all():
                    OrderItem.objects.create(
                        order=master_order,
                        food=item.food,
                        quantity=item.quantity,
                        price=item.price_at_addition * item.quantity
                    )
                    items_subtotal += (item.price_at_addition * item.quantity)
            
            master_order.items_subtotal = items_subtotal
            master_order.total_amount = items_subtotal + pool.total_delivery_fee
            master_order.save()
            
            pool.status = 'COMPLETED'
            pool.save()
            
            # 3. Publish Event
            EventBus.publish(OrderCreated(
                aggregate_id=master_order.id,
                payload={'customer_id': str(master_order.customer.id), 'is_pool': True}
            ))
            
            return master_order

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
            
            # Proactively try to batch this order with others in the same zone
            if order.zone:
                from domain.services.batching_service import BatchingService
                BatchingService.attempt_batching(order.restaurant.id, order.zone.id)
            
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
            driver.total_claims_accepted += 1
            driver.save(update_fields=['status', 'total_claims_accepted'])
            
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
            driver.total_earnings += order.delivery_fee
            driver.today_earnings += order.delivery_fee
            driver.save(update_fields=['status', 'total_earnings', 'today_earnings'])
            
            assignment = order.delivery_assignment
            from django.utils import timezone
            assignment.delivered_at = timezone.now()
            assignment.save()

            EventBus.publish(OrderDelivered(aggregate_id=order.id, payload={'actor_id': str(actor.id)}))
            return order

class AssignBatchService:
    @staticmethod
    def execute(batch_id, driver_id, actor):
        from apps.orders.models import DeliveryBatch
        import logging
        logger = logging.getLogger(__name__)
        
        with transaction.atomic():
            # 1. Lock the batch to prevent concurrent assignment
            batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
            
            # 2. Validation: Prevent double assignment or assigning non-ready batches
            if batch.status != 'CREATED':
                logger.warning(f"Batch {batch_id} is already {batch.status}. Skipping assignment.")
                return batch

            driver = Driver.objects.select_for_update().get(id=driver_id)
            
            # 3. Transition Batch
            batch.status = 'ASSIGNED'
            batch.driver = driver
            batch.save()
            
            # 4. Transition all orders in the batch
            # We use a sub-loop with error handling so one bad order doesn't kill the batch
            for order in batch.orders.all():
                try:
                    if not order.driver:
                        AssignDriverService.execute(order.id, driver.id, actor)
                except Exception as e:
                    logger.error(f"Failed to assign order {order.id} in batch {batch_id}: {str(e)}")
            
            return batch

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
                # 3. Dispatch: Prefer batch if it exists
                # AssignBatchService handles its own locking and state validation
                if order.batch:
                    AssignBatchService.execute(order.batch.id, best_driver.id, system_user)
                else:
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
