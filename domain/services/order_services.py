import logging
import random
import string
from decimal import Decimal
from datetime import timedelta

from django.db import transaction, IntegrityError
from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib.gis.measure import D
from django.core.cache import cache

# App Models
from apps.orders.models import Order, OrderItem, DeliveryBatch, OrderPool, PoolMember, PoolItem
from apps.delivery.models import Driver, DeliveryAssignment, DeliveryProof

from apps.restaurants.models import Restaurant, DeliveryZone
from apps.wallets.models import Wallet, WalletTransaction

# Domain Logic & FSM
from domain.fsm.order_fsm import OrderFSM, InvalidStateTransitionError
from domain.fsm.driver_fsm import DriverFSM
from domain.events.bus import EventBus
from domain.events.types import (
    OrderCreated, OrderConfirmed, DriverAssigned, OrderDelivered, 
    FoodReady, OrderPicked, OrderPreparing, OrderCancelled, DispatchFailed
)

# Services & Utils
from domain.services.pricing_service import PricingService
from domain.services.batching_service import BatchingService
from domain.services.payment_services import RefundPaymentService
from apps.events.utils import broadcast_order_status, broadcast_pool_status
from apps.core.geo_utils import ZONE_RESOLUTION, PROXIMITY_RESOLUTION, latlng_to_h3, get_neighbors
from api.serializers.utils import parse_coordinate_dict
from infrastructure.matching.strategies import PostgisRadiusStrategy

logger = logging.getLogger(__name__)
User = get_user_model()

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
        
        # Calculate dynamic delivery fee
        dynamic_fee = PricingService.calculate_delivery_fee(restaurant, delivery_location)
        
        # Tag Zone for Batching
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
        
        # 1. Prevent multiple OPEN pools
        if OrderPool.objects.filter(initiator=initiator, status='OPEN').exists():
            raise ValueError("You already have an open group order.")
            
        restaurant = Restaurant.objects.get(id=restaurant_id)
        
        # 2. Parse delivery location
        if delivery_location and not hasattr(delivery_location, 'x'):
            # If it's a dict or string, parse it
            parsed_loc = parse_coordinate_dict({'loc': delivery_location}, 'loc')
            if parsed_loc:
                delivery_location = parsed_loc
            else:
                raise ValueError("Invalid delivery location format. Expected lat/lng dict or POINT WKT.")
        
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
            broadcast_pool_status(str(pool.id), 'OPEN')

            
        return pool

    @staticmethod
    def join_pool(user, invite_code):
        pool = OrderPool.objects.get(invite_code=invite_code, status='OPEN')
        if pool.expires_at <= timezone.now():
            raise ValueError("This pool has expired.")
        member, created = PoolMember.objects.get_or_create(pool=pool, user=user)
        return pool

    @staticmethod
    def add_item(user, pool_id, food_id, quantity):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id, status='OPEN')
            if pool.expires_at <= timezone.now():
                raise ValueError("This pool has expired.")
            member = PoolMember.objects.select_for_update().get(pool=pool, user=user)
            
            # Using Food model (imported at top)
            from apps.products.models import Food
            food = Food.objects.get(id=food_id)
            
            if food.vendor != pool.restaurant:
                raise ValueError("Food must be from the same restaurant as the pool.")

            # Check if item already exists for this member
            pool_item, created = PoolItem.objects.get_or_create(
                member=member,
                food=food,
                defaults={'price_at_addition': food.price, 'quantity': 0}
            )
            
            pool_item.quantity += quantity
            pool_item.save()
            
            # Update member subtotal
            member.items_subtotal += (food.price * quantity)
            member.save()

            
            # Notify members of the update
            broadcast_pool_status(str(pool.id), 'OPEN', message=f"{user.username} added {food.name}")
            return pool

    @staticmethod
    def remove_item(user, pool_id, item_id):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id, status='OPEN')
            member = PoolMember.objects.select_for_update().get(pool=pool, user=user)
            item = PoolItem.objects.get(id=item_id, member=member)
            
            # Update member subtotal
            member.items_subtotal -= (item.price_at_addition * item.quantity)
            member.save()
            
            food_name = item.food.name
            item.delete()
            
            broadcast_pool_status(str(pool.id), 'OPEN', message=f"{user.username} removed {food_name}")
            return pool

    @staticmethod
    def mark_member_ready(user, pool_id):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id, status='OPEN')
            member = PoolMember.objects.select_for_update().get(pool=pool, user=user)
            if member.items_subtotal <= 0:
                raise ValueError("You must add at least one item before marking as ready.")
            
            member.status = 'READY'
            member.save()
            
            broadcast_pool_status(str(pool.id), 'OPEN', message=f"{user.username} is ready!")
            return pool


    @staticmethod
    def lock_pool(pool_id):
        """
        Calculate final fees and split the delivery cost among members.
        """
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
                PoolMember.objects.bulk_update(members, ['delivery_fee_share'])
            
            pool.save()
            return pool


    @staticmethod
    def finalize_pool(pool_id):
        """
        Convert the paid pool into a single Order for the vendor/rider.
        """
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)
            
            if pool.status != 'LOCKED':
                raise ValueError(f"Cannot finalize pool in {pool.status} state")
            
            # Check if all members have paid
            if pool.members.filter(status__in=['JOINED', 'READY']).exists():
                raise ValueError("All pool members must pay before finalization")
            
            pool_delivery_h3 = latlng_to_h3(pool.delivery_location.y, pool.delivery_location.x, ZONE_RESOLUTION)
            
            zone = DeliveryZone.objects.filter(
                restaurant=pool.restaurant, 
                is_active=True, 
                h3_index=pool_delivery_h3
            ).order_by('-priority').first()
            
            # 1. Create a single master Order
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
            
            # 2. Transfer all pool items to order items and build breakdown
            items_subtotal = Decimal('0.00')
            order_items_to_create = []
            group_breakdown = []
            
            for member in pool.members.select_related('user').prefetch_related('items', 'items__food').all():
                member_items = []
                for item in member.items.all():
                    order_items_to_create.append(OrderItem(
                        order=master_order,
                        food=item.food,
                        quantity=item.quantity,
                        price=item.price_at_addition * item.quantity
                    ))
                    member_items.append({
                        'food_name': item.food.name,
                        'quantity': item.quantity,
                        'price': float(item.price_at_addition)
                    })
                    items_subtotal += (item.price_at_addition * item.quantity)
                
                group_breakdown.append({
                    'username': member.user.username,
                    'phone': member.user.phone,
                    'items': member_items,
                    'subtotal': float(member.items_subtotal),
                    'delivery_share': float(member.delivery_fee_share)
                })
            
            OrderItem.objects.bulk_create(order_items_to_create)
            
            master_order.items_subtotal = items_subtotal
            master_order.total_amount = items_subtotal + pool.total_delivery_fee
            master_order.group_breakdown = group_breakdown
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
                try:
                    RefundPaymentService.execute(order.id)
                except Exception as e:
                    # Log but don't block cancellation; might need manual refund retry
                    logger.error(f"Automated refund failed for Order {order.id}: {str(e)}")

            # 3. Critical: Release driver back to AVAILABLE
            if order.driver:
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
            order.ready_at = timezone.now()
            order.save()
            
            # Proactively try to batch this order with others in the same zone
            if order.h3_index:
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
            order.save()
            
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
            logger.info(f"DEBUG PICKUP: order={order_id}, status={order.status}, "
                        f"order.driver={order.driver_id}, actor={actor.id} ({actor.role})")
            if actor.role != 'driver':
                raise PermissionError("Only drivers can perform order pickups.")
            
            # SELF-HEALING FALLBACK: If order is ASSIGNED but driver link is null (due to previous bug)
            if not order.driver and order.status == 'ASSIGNED':
                assignment = getattr(order, 'delivery_assignment', None)
                if assignment:
                    logger.info(f"HEALING: Restoring missing driver {assignment.driver_id} for order {order.id}")
                    order.driver = assignment.driver
                    order.save(update_fields=['driver'])
            
            if not getattr(order, 'driver', None) or order.driver.user != actor:
                logger.warning(f"DEBUG PICKUP REJECTED: order.driver={'None' if not order.driver else order.driver_id}, "
                               f"driver.user={'N/A' if not order.driver else order.driver.user_id}, actor={actor.id}")
                raise PermissionError("Only the assigned driver can pickup this order.")
            
            driver = actor.driver_profile
            if not bypass_geofence:
                if not driver.location or not order.restaurant.geo_location:
                    raise ValueError("Location data missing for geofence check")
                
                # STRICT: 20m geofence for pickup
                dist = driver.location.distance(order.restaurant.geo_location)
                if dist > 20:
                    raise ValueError(f"You are too far from the restaurant to pickup ({int(dist)}m away, 20m limit)")
            
            order.delivery_otp = ''.join([str(random.randint(0, 9)) for _ in range(4)])
            
            new_state = OrderFSM.transition(order.status, 'PICKED')
            order.status = new_state
            order.save()
            
            # Transition Driver to DELIVERING
            driver.status = DriverFSM.transition(driver.status, 'DELIVERING')
            driver.total_claims_accepted += 1
            driver.save(update_fields=['status', 'total_claims_accepted'])
            
            assignment = order.delivery_assignment
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
                
            # SELF-HEALING FALLBACK: If driver link is null, check delivery_assignment
            if not order.driver:
                assignment = getattr(order, 'delivery_assignment', None)
                if assignment:
                    logger.info(f"HEALING (DELIVER): Restoring missing driver {assignment.driver_id} for order {order.id}")
                    order.driver = assignment.driver
                    order.save(update_fields=['driver'])

            if not getattr(order, 'driver', None) or order.driver.user != actor:
                raise PermissionError("Only the assigned driver can complete this delivery.")
            
            if not otp or otp != order.delivery_otp:
                raise ValueError("Invalid delivery OTP. Please ask the customer for their 4-digit PIN.")

            driver = actor.driver_profile
            if not bypass_geofence:
                if not driver.location or not order.delivery_location:
                    raise ValueError("Location data missing for geofence check")
                
                # RESTORED: 100m geofence for delivery
                dist = driver.location.distance(order.delivery_location)
                if dist > 100:
                    raise ValueError(f"You are too far from the delivery location to mark as delivered ({int(dist)}m away)")
            
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
            assignment.delivered_at = timezone.now()
            assignment.save()

            EventBus.publish(OrderDelivered(aggregate_id=order.id, payload={'actor_id': str(actor.id)}))
            return order

class AssignBatchService:
    @staticmethod
    def execute(batch_id, driver_id, actor):
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
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            
            if order.driver:
                return order # Already assigned
            
            # 1. Get strategy (PostGIS preferred)
            strategy = PostgisRadiusStrategy()
            
            # 2. Find best driver based on restaurant location
            res = order.restaurant
            if res.geo_location:
                rlat, rlng = res.geo_location.y, res.geo_location.x
            elif res.latitude and res.longitude:
                rlat, rlng = res.latitude, res.longitude
            else:
                logger.error(f"Restaurant {res.id} has no coordinates. Dispatch failed.")
                return order

            best_driver = strategy.find_best_driver(
                restaurant_lat=rlat,
                restaurant_lng=rlng,
                max_radius_km=radius_km,
                restaurant_id=order.restaurant_id
            )
            
            if best_driver:
                system_user = cache.get('system_dispatcher_user')
                if not system_user:
                    system_user, _ = User.objects.get_or_create(
                        username='system_dispatcher',
                        defaults={'role': 'admin', 'is_staff': True}
                    )
                    cache.set('system_dispatcher_user', system_user, 3600)

                # 3. Dispatch: Offer order to the best driver
                if order.batch:
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
                
                logger.warning(f"No available drivers found for Order {order_id}.")
                return order
