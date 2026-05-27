from domain.events.bus import EventBus
from apps.payments.models import Payment
from domain.services.payment_services import ReleasePaymentService
from infrastructure.notifications.service import NotificationService
from domain.events.types import (
    OrderDelivered, PaymentReleaseRequested, PaymentCollected, 
    PaymentInitiated, PaymentDisbursed, PaymentRefunded, OrderConfirmed
)
from domain.services.order_services import ConfirmOrderService, DispatchService
import logging


logger = logging.getLogger(__name__)

def on_order_delivered(event: OrderDelivered):
    logger.info(f"Received OrderDelivered event for order {event.aggregate_id}")
    
    from apps.orders.models import Order
    try:
        order = Order.objects.select_related('customer').get(id=event.aggregate_id)
        # 1. Notify Customer
        NotificationService.send_push(
            user_id=str(order.customer.id),
            title="Delivery Complete",
            body=f"Your order {event.aggregate_id} has been delivered. Enjoy!",
            data={"order_id": str(order.id), "type": "delivery_complete"}
        )
    except Order.DoesNotExist:
        logger.error(f"Order {event.aggregate_id} not found for delivery notification")

    # 2. Trigger Payment Release
    try:
        if order.pool:
            # Group Order: Release funds for ALL members
            member_ids = order.pool.members.values_list('id', flat=True)
            member_payments = Payment.objects.filter(payable_type='pool_member', payable_id__in=member_ids)
            for p in member_payments:
                EventBus.publish(PaymentReleaseRequested(
                    aggregate_id=p.id, 
                    payload={'order_id': str(order.id)}
                ))
        else:
            # Standard Order
            payment = Payment.objects.get(payable_type='order', payable_id=event.aggregate_id)
            EventBus.publish(PaymentReleaseRequested(
                aggregate_id=payment.id, 
                payload={'order_id': str(event.aggregate_id)}
            ))
    except (Payment.DoesNotExist, Exception) as e:
        logger.error(f"Failed to trigger payment release for order {event.aggregate_id}: {str(e)}")


def on_payment_release_requested(event: PaymentReleaseRequested):
    logger.info(f"Releasing funds for payment {event.aggregate_id}")

    try:
        ReleasePaymentService.execute(event.aggregate_id)
        logger.info(f"Successfully released payment {event.aggregate_id}")
    except Exception as e:

        logger.error(f"Failed to release payment {event.aggregate_id}: {str(e)}")


def on_payment_collected(event: PaymentCollected):
    payable_type = event.payload.get('payable_type')
    payable_id = event.payload.get('payable_id')
    
    logger.info(f"Payment collected for {payable_type} {payable_id}")
    
    if payable_type != 'order':
        # Currently only orders need automated confirmation/dispatch triggers here
        # Marketplace and Pool Share activations are handled inside CompletePaymentService
        return

    try:
        from apps.orders.models import Order
        from django.db import transaction
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=payable_id)
            if order.status != 'CREATED':
                logger.info(f"Order {payable_id} already in {order.status} state. Skipping.")
                return
            
            order.is_paid = True
            order.save(update_fields=['is_paid', 'updated_at'])
            # Pass the customer as the actor for payment confirmation
            ConfirmOrderService.execute(payable_id, order.customer)
    except Exception as e:
        logger.error(f"Failed to confirm order after payment: {str(e)}")

def on_order_confirmed_dispatch(event: OrderConfirmed):
    """
    Trigger automated dispatch when an order is confirmed.
    Skips individual dispatch for orders with h3_index (zone-mapped orders)
    because they will be dispatched via the batching pipeline after FoodReady.
    Non-zone orders fall through to the original individual dispatch path.
    """
    from apps.orders.models import Order
    try:
        order = Order.objects.get(id=event.aggregate_id)
        if order.h3_index:
            logger.debug(f"Order {order.id} has h3_index — deferring to batching pipeline")
            return
    except Order.DoesNotExist:
        pass

    from domain.services.dispatch_tasks import dispatch_order_task
    dispatch_order_task.delay(str(event.aggregate_id))

def on_payment_initiated(event: PaymentInitiated):
    logger.info(f"Payment initiated for order {event.payload.get('order_id')}")
    # Optional: Send "Processing payment" notification or log it for analytics

def on_payment_disbursed(event: PaymentDisbursed):
    order_id = event.payload.get('order_id')
    logger.info(f"Funds disbursed for order {order_id}")
    
    from apps.orders.models import Order
    try:
        from django.db import transaction
        from apps.users.models import User
        from apps.payments.models import Transaction, Payment
        from domain.services.wallet_services import WalletDomainService
        
        # EDA Wallet Updates
        payment = Payment.objects.get(id=event.aggregate_id)
        restaurant_owner_id = event.payload.get('restaurant_owner_id')
        restaurant_payout_str = event.payload.get('restaurant_payout', '0')
        driver_user_id = event.payload.get('driver_user_id')
        driver_payout_str = event.payload.get('driver_payout', '0')
        
        from decimal import Decimal
        restaurant_payout = Decimal(restaurant_payout_str)
        driver_payout = Decimal(driver_payout_str)
        
        with transaction.atomic():
            if restaurant_owner_id and restaurant_payout > 0:
                restaurant_owner = User.objects.get(id=restaurant_owner_id)
                WalletDomainService.credit(restaurant_owner, restaurant_payout, f"Earnings from Order #{order_id}")
                Transaction.objects.create(
                    payment=payment, 
                    to_wallet=restaurant_owner, 
                    amount=restaurant_payout, 
                    transaction_type='platform_to_restaurant', 
                    status='COMPLETED'
                )
                
            if driver_user_id and driver_payout > 0:
                driver_user = User.objects.get(id=driver_user_id)
                WalletDomainService.credit(driver_user, driver_payout, f"Delivery for Order #{order_id}")
                Transaction.objects.create(
                    payment=payment, 
                    to_wallet=driver_user, 
                    amount=driver_payout, 
                    transaction_type='platform_to_driver', 
                    status='COMPLETED'
                )
                
        order = Order.objects.select_related('restaurant__owner', 'driver__user').get(id=order_id)
        # Notify Restaurant
        NotificationService.send_push(
            user_id=str(order.restaurant.owner.id),
            title="Funds Received",
            body=f"Payment for order {order_id} has been added to your wallet.",
            data={"order_id": str(order.id), "type": "payment_received"}
        )
        # Notify Driver if any
        if order.driver:
            NotificationService.send_push(
                user_id=str(order.driver.user.id),
                title="Delivery Earning",
                body=f"Earning for delivery {order_id} has been credited.",
                data={"order_id": str(order.id), "type": "payout_received"}
            )
    except Order.DoesNotExist:
        logger.error(f"Order {order_id} not found for disbursement notification")

def on_payment_refunded(event: PaymentRefunded):
    payable_id = event.payload.get('payable_id')
    logger.info(f"Payment refunded for {event.payload.get('type')} {payable_id}")
    
    if event.payload.get('type') != 'order':
        return # Currently only notifying order refunds
        
    order_id = payable_id
    from apps.orders.models import Order
    try:
        order = Order.objects.select_related('customer').get(id=order_id)
        NotificationService.send_push(
            user_id=str(order.customer.id),
            title="Refund Successful",
            body=f"A refund for your cancelled order {order_id} has been processed.",
            data={"order_id": str(order.id), "type": "refund_processed"}
        )
    except Order.DoesNotExist:
        logger.error(f"Order {order_id} not found for refund notification")

def on_auth_otp_requested(event):
    phone = event.payload.get('phone')
    otp = event.payload.get('otp')
    
    # Message template for OTP
    message = f"Your Orbit verification code is {otp}. It will expire in 5 minutes. Do not share this code with anyone."
    
    # Send SMS (will print to terminal if MOCK_SMS=True)
    NotificationService.send_sms(phone, message)
    
    logger.info(f"OTP request processed for {phone}")

def on_auth_user_verified(event):
    logger.info(f"User verified: {event.payload.get('phone')}")

def on_auth_user_registered(event):
    logger.info(f"New User registered: {event.payload.get('phone')} with username {event.payload.get('username')}")

def on_order_event(event):
    """
    Generic handler to broadcast any Order domain event to WebSockets
    """
    status_msg = f"Order status: {event.event_type}"
    from apps.events.utils import broadcast_order_status
    from apps.events.webhooks import fire_order_webhook
    logger.info(f"Broadcasting {event.event_type} for {event.aggregate_id}")
    
    # 2. Fetch Order and Push Notifications
    try:
        from apps.orders.models import Order
        order = Order.objects.select_related('customer', 'restaurant__owner', 'driver__user').get(id=event.aggregate_id)
        
        # Map event types to user-friendly messages (Now safe to use 'order' object)
        messages = {
            'OrderCreated': "Order placed successfully!",
            'OrderConfirmed': "Payment received! Restaurant is notified.",
            'OrderPreparing': "Restaurant has started preparing your food! ",
            'FoodReady': "Food is ready for pickup! ",
            'DriverAssigned': "A rider is on the way to pick up your food.",
            'OrderPicked': f"Rider has picked up your order! Your Delivery PIN: {getattr(order, 'delivery_otp', '####')}",
            'OrderDelivered': "Order delivered! Enjoy your meal ",
            'OrderCancelled': "Order has been cancelled. 🛑",
        }
        
        status_msg = messages.get(event.event_type, f"Order status: {event.event_type}")
        
        # 1. Internal WebSocket Broadcast
        broadcast_order_status(
            order_id=str(event.aggregate_id),
            status=event.event_type,
            message=status_msg
        )
        
        extra_data = {"order_id": str(order.id), "type": "order_update", "status": event.event_type}
        
        if event.event_type == 'OrderCreated':
            # Notify customer that order is pending payment
            if not event.payload.get('is_pool'):
                NotificationService.send_push(
                    user_id=str(order.customer.id),
                    title="Order Drafted",
                    body="Complete your payment to send the order to the restaurant.",
                    data=extra_data
                )
        elif event.event_type == 'OrderConfirmed':
            # 1. Notify Customer
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title="Order Confirmed",
                body=status_msg,
                data=extra_data
            )
            # 2. Notify Vendor (Now only after payment)
            NotificationService.send_push(
                user_id=str(order.restaurant.owner.id),
                title="New Group Order!" if order.group_breakdown else "New Order!",
                body=f"You have a new paid order: #{str(order.id).split('-')[0]}",
                data=extra_data
            )
            # 3. SMS Notification to Vendor (Detailed for Group Orders)
            vendor_phone = order.restaurant.owner.phone
            vendor_name = order.restaurant.owner.username or "Vendor"
            
            if order.group_breakdown:
                sms_text = f"Habari {vendor_name}, una oda mpya ya KIKUNDI #{str(order.id).split('-')[0]}.\n"
                for member in order.group_breakdown:
                    items_str = ", ".join([f"{i['quantity']}x {i['food_name']}" for i in member['items']])
                    sms_text += f"- {member['username']}: {items_str}\n"
                sms_text += "Tafadhali pakia kila mteja kivyake kwa urahisi wa usambazaji."
            else:
                sms_text = f"Habari {vendor_name}, una oda mpya #{str(order.id).split('-')[0]} kwenye Mwanachuoshop. Tafadhali iandae sasa."
                
            NotificationService.send_sms(vendor_phone, sms_text)

        elif event.event_type in ['OrderPreparing', 'FoodReady', 'DriverAssigned', 'OrderPicked']:
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title="Order Update",
                body=status_msg,
                data=extra_data
            )
        elif event.event_type == 'OrderCancelled':
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title="Order Cancelled",
                body=status_msg,
                data=extra_data
            )
    except Exception as e:
        logger.error(f"Push notification failed for {event.event_type}: {str(e)}")
    
    # 3. External Webhook Broadcast
    payload = event.payload.copy()
    payload['message'] = status_msg
    payload['aggregate_id'] = str(event.aggregate_id)
    fire_order_webhook.delay(event.event_type, payload)

def on_food_ready_cross_batching(event):
    """
    Trigger unified batching when a regular (non-pool) order becomes READY.
    Pool orders are dispatched via on_pool_ready instead.
    Falls back to direct dispatch if batching produces no result.
    """
    from apps.orders.models import Order
    from domain.services.batch_pipeline import BatchPipeline
    from domain.services.dispatch_tasks import dispatch_order_task

    try:
        order = Order.objects.select_related('restaurant').get(id=event.aggregate_id)
        # Skip pool orders — they go through on_pool_ready
        if getattr(order, 'is_pool_order', False):
            return

        batched = False
        if order.h3_index:
            result = BatchPipeline.run(order)
            if result:
                logger.info(f"Batching succeeded: created {result.batch_type} batch {result.id} for order {order.id}")
                batched = True
            else:
                logger.info(f"No batch created for order {order.id} — falling back to direct dispatch")

        if not batched:
            logger.info(f"Direct dispatch triggered for order {order.id}")
            dispatch_order_task.delay(str(order.id))
    except Exception as e:
        logger.error(f"Error in batching handler: {str(e)}")


def on_order_status_update_batch_lifecycle(event):
    """
    Manages the lifecycle of a DeliveryBatch based on its member orders.
    """
    from apps.orders.models import Order, DeliveryBatch
    from django.db import transaction
    
    try:
        # 1. Fetch order and check if it belongs to a batch
        order = Order.objects.select_related('batch').get(id=event.aggregate_id)
        if not order.batch:
            return

        batch = order.batch
        with transaction.atomic():
            # 2. Lock the batch for state transition
            batch = DeliveryBatch.objects.select_for_update().get(id=batch.id)
            
            if event.event_type == 'OrderPicked':
                # Transition Batch to PICKED if it was just ASSIGNED
                if batch.status == 'ASSIGNED':
                    batch.status = 'PICKED'
                    batch.save()
                    logger.info(f"Batch {batch.id} transitioned to PICKED")
            
            elif event.event_type in ['OrderDelivered', 'OrderCancelled']:
                # 3. Check if ALL orders in the batch have reached a terminal state
                terminal_states = ['DELIVERED', 'CANCELLED']
                remaining_active = batch.orders.exclude(status__in=terminal_states).exists()
                
                if not remaining_active:
                    # If any order was delivered, the batch as a whole is COMPLETED
                    if batch.orders.filter(status='DELIVERED').exists():
                        batch.status = 'COMPLETED'
                    else:
                        # If everything was cancelled, the batch is CANCELLED
                        batch.status = 'CANCELLED'
                    
                    batch.save()
                    logger.info(f"Batch {batch.id} transitioned to {batch.status}")
                    
    except Order.DoesNotExist:
        pass
    except Exception as e:
        logger.error(f"Error in batch lifecycle handler: {str(e)}")


def on_batch_offered(event):
    """Handle batch offered to driver"""
    logger.info(f"Batch {event.aggregate_id} offered to driver")


def on_batch_accepted(event):
    """Handle driver accepted batch - publish DriverAssigned for each order"""
    from apps.orders.models import DeliveryBatch
    from domain.events.types import DriverAssigned

    try:
        batch = DeliveryBatch.objects.select_related('driver').get(id=event.aggregate_id)
        if batch.driver:
            for order in batch.orders.all():
                EventBus.publish(DriverAssigned(
                    aggregate_id=order.id,
                    payload={'driver_id': str(batch.driver.id), 'batch_id': str(batch.id)}
                ))
            logger.info(f"Batch {batch.id} accepted, published DriverAssigned for {batch.orders.count()} orders")
    except Exception as e:
        logger.error(f"Error in on_batch_accepted handler: {str(e)}")


def on_batch_declined(event):
    """Handle driver declined batch - already handled by re-offer in BatchDispatchService"""
    logger.info(f"Batch {event.aggregate_id} declined, re-offer triggered")


def on_batch_expired(event):
    """Handle batch offer expired - log and let dispatch service handle re-offer"""
    logger.info(f"Batch {event.aggregate_id} offer expired, reason: {event.payload.get('reason')}")


def on_temp_driver_registered(event):
    """
    When a temporary driver is registered, check for waiting batches
    from the same restaurant and try to assign the driver.
    Uses database-level locking to handle race conditions.
    """
    from django.db import transaction, models
    from django.db.models import Q, Count
    from apps.delivery.models import Driver
    from apps.orders.models import DeliveryBatch
    from domain.fsm.driver_fsm import DriverFSM

    driver_id = event.payload.get('driver_id')
    restaurant_id = event.payload.get('assigned_by')

    if not driver_id or not restaurant_id:
        logger.warning(f"TempDriverRegistered event missing driver_id or assigned_by: {event.payload}")
        return

    logger.info(f"Checking for waiting batches for restaurant {restaurant_id} after temp driver {driver_id} registered")

    with transaction.atomic():
        batch = DeliveryBatch.objects.select_for_update(skip_locked=True).filter(
            status='WAITING_TEMP_DRIVER',
            batch_type='SINGLE_RESTAURANT'
        ).filter(
            orders__restaurant_id=restaurant_id
        ).annotate(
            order_count=Count('orders')
        ).order_by('waiting_since').first()

        if not batch:
            logger.info(f"No waiting batches found for restaurant {restaurant_id}")
            return

        if batch.driver_id:
            logger.info(f"Batch {batch.id} already has a driver assigned, skipping")
            return

        try:
            driver = Driver.objects.select_for_update().get(id=driver_id)

            if driver.status not in ['OFFLINE', 'AVAILABLE']:
                logger.warning(f"Driver {driver_id} is in status {driver.status}, cannot assign to batch")
                return

            if driver.driver_type != 'temporary' or str(driver.assigned_by_id) != str(restaurant_id):
                logger.warning(f"Driver {driver_id} not assigned to restaurant {restaurant_id}")
                return

            order_count = batch.orders.count()

            if driver.current_active_orders + order_count > driver.max_active_orders:
                logger.warning(
                    f"[TempDriver] Driver {driver_id} would exceed capacity "
                    f"({driver.current_active_orders} + {order_count} > {driver.max_active_orders}). "
                    f"Skipping batch {batch.id}"
                )
                return

            batch.driver = driver
            batch.status = 'ASSIGNED'
            batch.save()

            if driver.status == 'OFFLINE':
                driver.status = DriverFSM.transition(driver.status, 'AVAILABLE')
            driver.status = DriverFSM.transition(driver.status, 'ASSIGNED')
            driver.current_active_orders += order_count
            driver.save()

            for order in batch.orders.all():
                order.status = 'ASSIGNED'
                order.driver = driver

            from apps.orders.models import Order
            Order.objects.bulk_update(list(batch.orders.all()), ['status', 'driver'])

            notify_other_restaurants_in_batch(batch.id, batch, restaurant_id)

            logger.info(f"Successfully assigned temp driver {driver_id} to batch {batch.id}")

            from domain.events.types import BatchAccepted
            EventBus.publish(BatchAccepted(
                aggregate_id=str(batch.id),
                payload={'driver_id': str(driver.id), 'restaurant_id': str(restaurant_id)}
            ))

        except Driver.DoesNotExist:
            logger.error(f"Driver {driver_id} not found")
        except Exception as e:
            logger.error(f"Error assigning temp driver to batch: {str(e)}")


def notify_other_restaurants_in_batch(batch_id, batch, assigned_restaurant_id):
    """Notify other restaurants in the batch that their orders are being delivered."""
    from apps.restaurants.models import Restaurant

    try:
        restaurant_ids = set()
        for order in batch.orders.all():
            if order.restaurant_id:
                restaurant_ids.add(order.restaurant_id)

        restaurant_ids.discard(assigned_restaurant_id)

        for rest_id in restaurant_ids:
            try:
                restaurant = Restaurant.objects.get(id=rest_id)
                if restaurant.owner:
                    from infrastructure.notifications.service import NotificationService
                    NotificationService.send_push(
                        user_id=str(restaurant.owner.id),
                        title="Orders Being Delivered! 🚗",
                        body=f"Your orders in batch {batch_id[:8]}... are now being delivered by a driver.",
                        data={"batch_id": str(batch_id), "type": "batch_delivering"}
                    )
            except Exception as e:
                logger.error(f"Failed to notify restaurant {rest_id}: {e}")
    except Exception as e:
        logger.error(f"Error notifying other restaurants: {e}")


def on_pool_cancelled(event):
    """
    Handle PoolCancelled event.
    If a batch was already in a non-cancellable state (PICKED_UP, DELIVERED),
    alert admins so they can handle it manually.
    """
    batch_id = event.payload.get('batch_id')
    pool_id = event.payload.get('pool_id')

    if not batch_id:
        return  # Pool was cancelled before it was ever finalised — nothing to do

    from apps.orders.models import DeliveryBatch
    try:
        batch = DeliveryBatch.objects.get(id=batch_id)
        if batch.status in ('PICKED_UP', 'PICKED', 'DELIVERED', 'COMPLETED'):
            logger.warning(
                f"[PoolCancelled] Pool {pool_id} cancelled but batch {batch_id} "
                f"is already in '{batch.status}' — manual intervention required."
            )
            NotificationService.notify_admins(
                title="Pool Cancelled — Batch In Transit ⚠️",
                body=(
                    f"Pool {str(pool_id)[:8]}... was cancelled but its batch "
                    f"{str(batch_id)[:8]}... is already '{batch.status}'. "
                    f"Manual intervention required."
                ),
                data={
                    'pool_id': str(pool_id),
                    'batch_id': str(batch_id),
                    'type': 'pool_cancel_late',
                },
            )
    except DeliveryBatch.DoesNotExist:
        pass  # Batch already gone — nothing to do


def on_dispatch_failed(event):
    order_id = event.payload.get('order_id')
    attempt_retry = event.payload.get('attempt_retry', True)
    logger.info(f"Dispatch failed for order {order_id}")

    from apps.events.utils import broadcast_order_status
    broadcast_order_status(
        order_id=str(order_id),
        status='DispatchFailed',
        message="Searching for a rider..." if attempt_retry else "No riders available. Please contact support."
    )

    NotificationService.notify_admins(
        title="Dispatch Alert",
        body=f"Order {order_id} failed to dispatch automatically.",
        data={"order_id": str(order_id), "type": "dispatch_failure"}
    )


def on_pool_ready(event):
    """
    Trigger the batching pipeline when a pool is marked ready by the restaurant.
    The pool is treated as a single dispatch unit — no individual order dispatch.
    """
    from apps.orders.models import OrderPool
    from domain.services.batch_pipeline import BatchPipeline
    from django.db import transaction

    pool_id = event.aggregate_id
    try:
        pool = OrderPool.objects.select_related('restaurant').get(id=pool_id)
        if not pool.h3_index:
            logger.debug(f"[PoolReady] Pool {pool_id} has no h3_index — skipping batching")
            return

        pool_ref = _PoolBatchable(pool)
        result = BatchPipeline.run_for_pool(pool_ref)
        if result:
            with transaction.atomic():
                # Re-fetch with lock to safely update status
                pool = OrderPool.objects.select_for_update().get(id=pool_id)
                if pool.status == 'READY':
                    pool.batch = result
                    pool.status = 'DISPATCHING'
                    pool.save(update_fields=['batch', 'status'])
            logger.info(f"[PoolReady] Pool {pool_id} dispatched via batch {result.id}")
        else:
            logger.debug(f"[PoolReady] No batch formed for pool {pool_id}, will retry in poll cycle")
    except Exception as e:
        logger.error(f"[PoolReady] Error processing pool {pool_id}: {e}", exc_info=True)


class _PoolBatchable:
    """
    Adapter that makes an OrderPool look like an Order to the BatchPipeline.
    The pipeline needs: h3_index, restaurant, delivery_location, id, customer,
    delivery_fee, service_fee, status, batch, batch_id, ready_at, created_at.
    """
    def __init__(self, pool):
        self.id = pool.id
        self.h3_index = pool.h3_index
        self.restaurant = pool.restaurant
        self.restaurant_id = pool.restaurant_id
        self.delivery_location = pool.delivery_location
        self.delivery_address = pool.delivery_address
        self.customer = pool.initiator
        self.customer_id = pool.initiator_id
        self.status = 'READY'
        self.batch = None
        self.batch_id = None
        self.ready_at = pool.updated_at
        self.created_at = pool.created_at
        self.is_pool = True
        self.is_pool_order = True
        # Financials needed by _create_single_order_batch_atomic pricing
        self.delivery_fee = pool.total_delivery_fee
        self.service_fee = pool.service_fee


def register_all_handlers():
    # Order Events
    from domain.events.types import (
        OrderCreated, OrderConfirmed, OrderPreparing, FoodReady, 
        DriverAssigned, OrderPicked, OrderDelivered, OrderCancelled,
        DispatchFailed
    )
    order_events = [
        OrderCreated, OrderConfirmed, OrderPreparing, FoodReady, 
        DriverAssigned, OrderPicked, OrderDelivered, OrderCancelled,
        DispatchFailed
    ]
    for event_type in order_events:
        EventBus.subscribe(event_type, on_order_event)

    # Specific Handlers
    EventBus.subscribe(OrderConfirmed, on_order_confirmed_dispatch)
    EventBus.subscribe(OrderDelivered, on_order_delivered)
    EventBus.subscribe(PaymentReleaseRequested, on_payment_release_requested)
    EventBus.subscribe(DispatchFailed, on_dispatch_failed)
    
    # Batch Lifecycle
    EventBus.subscribe(OrderPicked, on_order_status_update_batch_lifecycle)
    EventBus.subscribe(OrderDelivered, on_order_status_update_batch_lifecycle)
    EventBus.subscribe(OrderCancelled, on_order_status_update_batch_lifecycle)

    # Cross-Restaurant Batching
    from domain.events.types import FoodReady
    EventBus.subscribe(FoodReady, on_food_ready_cross_batching)

    # Batch Dispatch Events
    from domain.events.types import BatchOffered, BatchAccepted, BatchDeclined, BatchExpired
    EventBus.subscribe(BatchOffered, on_batch_offered)
    EventBus.subscribe(BatchAccepted, on_batch_accepted)
    EventBus.subscribe(BatchDeclined, on_batch_declined)
    EventBus.subscribe(BatchExpired, on_batch_expired)

    # Temp Driver Events
    from domain.events.types import TempDriverRegistered
    EventBus.subscribe(TempDriverRegistered, on_temp_driver_registered)

    # Pool lifecycle events
    from domain.events.types import PoolCancelled, PoolReady
    EventBus.subscribe(PoolCancelled, on_pool_cancelled)
    EventBus.subscribe(PoolReady, on_pool_ready)

    # Other Domain Events
    from domain.events.types import PaymentCollected, AuthOTPRequested, AuthUserVerified, AuthUserRegistered
    EventBus.subscribe(PaymentCollected, on_payment_collected)
    EventBus.subscribe(PaymentInitiated, on_payment_initiated)
    EventBus.subscribe(PaymentDisbursed, on_payment_disbursed)
    EventBus.subscribe(PaymentRefunded, on_payment_refunded)
    EventBus.subscribe(AuthOTPRequested, on_auth_otp_requested)
    EventBus.subscribe(AuthUserVerified, on_auth_user_verified)
    EventBus.subscribe(AuthUserRegistered, on_auth_user_registered)

