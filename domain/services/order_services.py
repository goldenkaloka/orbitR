import logging

from django.db import transaction
from django.utils import timezone
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
from apps.core.geo_utils import ZONE_RESOLUTION, PROXIMITY_RESOLUTION, latlng_to_h3, get_neighbors
import h3

logger = logging.getLogger(__name__)


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
        delivery_h3 = None
        if delivery_location:
            delivery_h3 = latlng_to_h3(delivery_location.y, delivery_location.x, ZONE_RESOLUTION)
            zone = DeliveryZone.objects.filter(
                restaurant=restaurant, 
                is_active=True, 
                h3_index=delivery_h3
            ).order_by('-priority').first()
        
        otp = str(random.randint(1000, 9999))
        
        if not restaurant.is_active:
            raise ValueError(f"Restaurant {restaurant.name} is currently not accepting orders.")

        # 2. Prepare items and calculate subtotal
        items_subtotal = Decimal('0.00')
        for item_data in items_data:
            food = item_data['food']
            if not food.is_available:
                raise ValueError(f"Item {food.name} is currently unavailable.")
            
            quantity = item_data['quantity']
            items_subtotal += (food.price * quantity)
            
        total_amount = items_subtotal + dynamic_fee
        otp = str(random.randint(1000, 9999))
        
        with transaction.atomic():
            # 1. Create Order with calculated amounts, OTP, and Zone
            order = Order.objects.create(
                items_subtotal=items_subtotal,
                delivery_fee=dynamic_fee, 
                total_amount=total_amount,
                delivery_otp=otp,
                zone=zone,
                h3_index=delivery_h3,
                **validated_data
            )
            
            # 2. Bulk create items
            order_items = []
            for item_data in items_data:
                food = item_data['food']
                quantity = item_data['quantity']
                order_items.append(OrderItem(
                    order=order,
                    food=food,
                    quantity=quantity,
                    price=food.price * quantity
                ))
            OrderItem.objects.bulk_create(order_items)
            
            # 3. Trigger Event (On commit to avoid race conditions)
            transaction.on_commit(lambda: EventBus.publish(OrderCreated(
                aggregate_id=order.id,
                payload={'customer_id': str(order.customer.id)}
            )))
        
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
        from django.db import IntegrityError
        from apps.events.utils import broadcast_order_status
        from apps.core.geo_utils import latlng_to_h3

        # 1. Prevent multiple OPEN pools
        if OrderPool.objects.filter(initiator=initiator, status='OPEN').exists():
            raise ValueError("You already have an open group order.")
            
        restaurant = Restaurant.objects.get(id=restaurant_id)
        
        # 2. Move parsing outside transaction
        if isinstance(delivery_location, dict):
            from api.serializers.utils import parse_coordinate_dict
            delivery_location = parse_coordinate_dict({'loc': delivery_location}, 'loc')
            if not delivery_location:
                raise ValueError("Valid delivery location (lat/lng) is required for pooling.")
                
        h3_index = None
        if delivery_location:
            h3_index = latlng_to_h3(delivery_location.y, delivery_location.x)
            
        # 3. Invite code retry logic
        max_retries = 3
        pool = None
        for i in range(max_retries):
            invite_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            try:
                with transaction.atomic():
                    pool = OrderPool.objects.create(
                        initiator=initiator,
                        restaurant=restaurant,
                        invite_code=invite_code,
                        delivery_address=delivery_address,
                        delivery_location=delivery_location,
                        h3_index=h3_index,
                        expires_at=timezone.now() + timedelta(hours=2)
                    )
                    PoolMember.objects.create(pool=pool, user=initiator)
                break
            except IntegrityError:
                if i == max_retries - 1:
                    raise ValueError("Failed to generate a unique invite code. Please try again.")
                continue
                
        # 4. WebSocket notification
        if pool:
            broadcast_order_status(str(pool.id), 'OPEN', is_pool=True)
            
        return pool

    @staticmethod
    def join_pool(user, invite_code):
        from apps.orders.models import OrderPool, PoolMember
        pool = OrderPool.objects.get(invite_code=invite_code, status='OPEN')
        if pool.expires_at <= timezone.now():
            raise ValueError("This pool has expired.")
        member, created = PoolMember.objects.get_or_create(pool=pool, user=user)
        return pool

    @staticmethod
    def add_item(user, pool_id, food_id, quantity):
        from apps.orders.models import OrderPool, PoolMember, PoolItem
        from apps.products.models import Food
        
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id, status='OPEN')
            if pool.expires_at <= timezone.now():
                raise ValueError("This pool has expired.")
            member = PoolMember.objects.select_for_update().get(pool=pool, user=user)
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
            members = list(pool.members.all())
            if members:
                share = total_fee / len(members)
                for member in members:
                    member.delivery_fee_share = share
                from apps.orders.models import PoolMember
                PoolMember.objects.bulk_update(members, ['delivery_fee_share'])
            
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
                # Security: Call finalize on commit to prevent race conditions or rollbacks
                # that would leave members marked as PAID but order not created.
                transaction.on_commit(lambda: PoolOrderService.finalize_pool(pool.id))
            
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
            
            if pool.status != 'LOCKED':
                raise ValueError(f"Cannot finalize pool in {pool.status} state")
            
            # Check if all members have paid
            if pool.members.filter(status__in=['JOINED', 'READY']).exists():
                raise ValueError("All pool members must pay before finalization")
            
            pool_delivery_h3 = latlng_to_h3(pool.delivery_location.y, pool.delivery_location.x, ZONE_RESOLUTION)
            
            from apps.restaurants.models import DeliveryZone
            zone = DeliveryZone.objects.filter(
                restaurant=pool.restaurant, 
                is_active=True, 
                h3_index=pool_delivery_h3
            ).order_by('-priority').first()
            
            # 1. Create a single master Order
            import random
            otp = str(random.randint(1000, 9999))
            
            master_order = Order.objects.create(
                customer=pool.initiator, # Initiator is the primary contact
                restaurant=pool.restaurant,
                delivery_address=pool.delivery_address,
                delivery_location=pool.delivery_location,
                delivery_fee=pool.total_delivery_fee,
                delivery_otp=otp,
                pool=pool,
                zone=zone,
                h3_index=pool_delivery_h3,
                status='CREATED',
                is_paid=True
            )
            
            # 2. Transfer all pool items to order items
            items_subtotal = Decimal('0.00')
            order_items_to_create = []
            
            for member in pool.members.prefetch_related('items').all():
                for item in member.items.all():
                    order_items_to_create.append(OrderItem(
                        order=master_order,
                        food=item.food,
                        quantity=item.quantity,
                        price=item.price_at_addition * item.quantity
                    ))
                    items_subtotal += (item.price_at_addition * item.quantity)
            
            OrderItem.objects.bulk_create(order_items_to_create)
            
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
            
            if not order.is_paid:
                raise ValueError("Cannot confirm an unpaid order")
                
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
            
            # Role-based validation
            if actor.role == 'customer' and order.customer == actor:
                if order.status not in ['CREATED', 'CONFIRMED']:
                    raise PermissionError("Customers can only cancel orders before they are prepared.")
            elif actor.role == 'vendor' and order.restaurant.owner == actor:
                pass # Vendors can cancel their own orders (e.g. out of stock)
            elif actor.role == 'admin':
                pass
            else:
                raise PermissionError("You do not have permission to cancel this order.")
            
            new_state = OrderFSM.transition(order.status, 'CANCELLED')
            order.status = new_state
            
            # 2. Critical: Refund if already paid
            if order.is_paid:
                from domain.services.payment_services import RefundPaymentService
                try:
                    RefundPaymentService.execute(order.id)
                except Exception as e:
                    # Log but don't block cancellation; might need manual refund retry
                    logger.error(f"Automated refund failed for Order {order.id}: {str(e)}")

            # 3. Critical: Release driver back to AVAILABLE
            if order.driver:
                from domain.fsm.driver_fsm import DriverFSM
                if order.driver.status != 'AVAILABLE':
                    order.driver.status = DriverFSM.transition(order.driver.status, 'AVAILABLE')
                order.driver.current_active_orders = max(0, order.driver.current_active_orders - 1)
                order.driver.save(update_fields=['status', 'current_active_orders'])
                DeliveryAssignment.objects.filter(order=order).delete()
            
            order.save()
            
            transaction.on_commit(lambda: EventBus.publish(OrderCancelled(aggregate_id=order.id, payload={
                'actor_id': str(actor.id),
                'reason': reason
            })))
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
            if order.h3_index:
                from domain.services.batching_service import BatchingService
                BatchingService.attempt_batching(order.restaurant.id, order.h3_index)
            
            EventBus.publish(FoodReady(aggregate_id=order.id, payload={'actor_id': str(actor.id)}))
            return order

class OfferOrderService:
    @staticmethod
    def execute(order_id, driver_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if order.driver:
                # If already assigned/offered to THIS driver, just return. 
                # If assigned to someone else, it's an error.
                if order.driver_id == driver_id:
                    return order
                raise ValueError("This order already has a driver assigned or offered.")
            
            driver = Driver.objects.select_for_update().get(id=driver_id)
            
            # Transition Order to OFFERED
            new_state = OrderFSM.transition(order.status, 'OFFERED')
            order.status = new_state
            order.driver = driver # Tentative assignment
            order.save()
            
            # Notify driver via Event/WebSocket
            transaction.on_commit(lambda: broadcast_order_status(str(order.id), 'OFFERED'))
            return order

class AcceptOrderService:
    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if order.status != 'OFFERED':
                raise ValueError(f"Cannot accept order in state {order.status}")
            
            if order.driver.user != actor:
                raise PermissionError("Only the offered driver can accept this order.")
            
            # Finalize via AssignDriverService
            return AssignDriverService.execute(order.id, order.driver.id, actor)

class RejectOrderService:
    @staticmethod
    def execute(order_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            if order.status != 'OFFERED':
                raise ValueError(f"Cannot reject order in state {order.status}")
            
            if order.driver.user != actor:
                raise PermissionError("Only the offered driver can reject this order.")
            
            driver = order.driver
            
            # Transition back to READY
            new_state = OrderFSM.transition(order.status, 'READY')
            order.status = new_state
            order.driver = None
            order.save()
            
            # Log refusal for metrics
            driver.total_claim_attempts += 1
            driver.save(update_fields=['total_claim_attempts'])
            
            # Re-trigger dispatch immediately to find the next driver
            from domain.services.dispatch_tasks import dispatch_order_task
            transaction.on_commit(lambda: dispatch_order_task.delay(str(order.id)))
            
            return order

class AssignDriverService:
    @staticmethod
    def execute(order_id, driver_id, actor):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            # Allow if it's already offered to this driver or if it's currently unassigned
            if order.driver and order.driver_id != driver_id:
                raise ValueError("This order already has a different driver assigned.")
            
            driver = Driver.objects.select_for_update().get(id=driver_id)
            if driver.driver_type == 'temporary' and driver.assigned_by_id != order.restaurant_id:
                raise PermissionError("Temporary drivers can only be assigned to their own restaurant orders.")
            
            # Transition Order
            new_state = OrderFSM.transition(order.status, 'ASSIGNED')
            order.status = new_state
            order.driver = driver
            
            # Transition Driver: Skip if already ASSIGNED (e.g. in a batch)
            if driver.status != 'ASSIGNED':
                driver.status = DriverFSM.transition(driver.status, 'ASSIGNED')
            driver.current_active_orders += 1
            driver.save(update_fields=['status', 'current_active_orders'])
            
            # Ensure assignment record exists
            DeliveryAssignment.objects.update_or_create(
                order=order, 
                defaults={'driver': driver}
            )
            
            transaction.on_commit(lambda: EventBus.publish(DriverAssigned(
                aggregate_id=order.id, 
                payload={'driver_id': str(driver.id), 'actor_id': str(actor.id)}
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
                
                # TESTING: Increased geofence to ~3km using ZONE_RESOLUTION and k=10
                driver_h3 = latlng_to_h3(driver.location.y, driver.location.x, ZONE_RESOLUTION)
                res_h3 = latlng_to_h3(order.restaurant.geo_location.y, order.restaurant.geo_location.x, ZONE_RESOLUTION)
                
                if driver_h3 != res_h3 and res_h3 not in get_neighbors(driver_h3, 10):
                    raise ValueError("You are too far from the restaurant to pickup (3km limit)")
            
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
            if actor.role != 'driver':
                raise PermissionError("Only drivers can perform delivery completions.")
                
            if not getattr(order, 'driver', None) or order.driver.user != actor:
                raise PermissionError("Only the assigned driver can complete this delivery.")
            
            if not otp or otp != order.delivery_otp:
                raise ValueError("Invalid delivery OTP. Please ask the customer for their 4-digit PIN.")

            driver = actor.driver_profile
            if not bypass_geofence:
                if not driver.location or not order.delivery_location:
                    raise ValueError("Location data missing for geofence check")
                
                driver_h3 = latlng_to_h3(driver.location.y, driver.location.x, PROXIMITY_RESOLUTION)
                dest_h3 = latlng_to_h3(order.delivery_location.y, order.delivery_location.x, PROXIMITY_RESOLUTION)
                
                # Use wrapper to get neighbors including self
                if driver_h3 != dest_h3 and dest_h3 not in get_neighbors(driver_h3, 1):
                    raise ValueError("You are too far from the delivery location to mark as delivered")
            
            new_state = OrderFSM.transition(order.status, 'DELIVERED')
            order.status = new_state
            order.save()
            
            # Transition Driver back to AVAILABLE
            driver.status = DriverFSM.transition(driver.status, 'AVAILABLE')
            driver.total_earnings += order.delivery_fee
            driver.today_earnings += order.delivery_fee
            driver.current_active_orders = max(0, driver.current_active_orders - 1)
            driver.save(update_fields=['status', 'total_earnings', 'today_earnings', 'current_active_orders'])
            
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
            # If any assignment fails, the outer transaction will roll back the whole batch
            for order in batch.orders.all():
                if order.driver != driver:
                    AssignDriverService.execute(order.id, driver.id, actor)
            
            return batch

class DispatchService:
    @staticmethod
    def execute(order_id, is_retry=False, radius_km=15.0):
        """
        The core of the Automated Dispatch Logic.
        Finds the closest available driver and assigns them to the order.
        """
        from infrastructure.matching.strategies import PostgisRadiusStrategy
        from django.contrib.auth import get_user_model
        from domain.events.types import DispatchFailed
        User = get_user_model()
        
        from django.core.cache import cache
        system_user = cache.get('system_dispatcher_user')
        if not system_user:
            system_user, _ = User.objects.get_or_create(
                username='system_dispatcher',
                defaults={'role': 'admin', 'is_staff': True}
            )
            cache.set('system_dispatcher_user', system_user, 3600)

        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            
            if order.driver:
                return order # Already assigned
            
            # 1. Get strategy (PostGIS preferred)
            strategy = PostgisRadiusStrategy()
            
            # 2. Find best driver based on restaurant location
            # Prefer PostGIS Point field over legacy Decimals
            res = order.restaurant
            if res.geo_location:
                rlat, rlng = res.geo_location.y, res.geo_location.x
            elif res.latitude and res.longitude:
                rlat, rlng = res.latitude, res.longitude
            else:
                logging.getLogger(__name__).error(f"Restaurant {res.id} has no coordinates. Dispatch failed.")
                return order

            best_driver = strategy.find_best_driver(
                restaurant_lat=rlat,
                restaurant_lng=rlng,
                max_radius_km=radius_km,
                restaurant_id=order.restaurant_id
            )
            
            if best_driver:
                # 3. Dispatch: Offer order to the best driver
                if order.batch:
                    # Batches still use direct assignment for now as they are admin-orchestrated
                    AssignBatchService.execute(order.batch.id, best_driver.id, system_user)
                else:
                    OfferOrderService.execute(
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
