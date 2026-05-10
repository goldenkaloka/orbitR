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
    Restored from FoodReady to allow driver travel to overlap with prep time.
    """
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
    message = f"Your Mwanachuo verification code is {otp}. It will expire in 5 minutes. Do not share this code with anyone."
    
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
            'OrderPreparing': "Restaurant has started preparing your food! 🔥",
            'FoodReady': "Food is ready for pickup! 🛵",
            'DriverAssigned': "A rider is on the way to pick up your food.",
            'OrderPicked': f"Rider has picked up your order! 🚀 Your Delivery PIN: {getattr(order, 'delivery_otp', '####')}",
            'OrderDelivered': "Order delivered! Enjoy your meal 😋",
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
    
    # Notify Admins
    NotificationService.notify_admins(
        title="Dispatch Alert",
        body=f"Order {order_id} failed to dispatch automatically.",
        data={"order_id": str(order_id), "type": "dispatch_failure"}
    )

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
    
    # Other Domain Events
    from domain.events.types import PaymentCollected, AuthOTPRequested, AuthUserVerified, AuthUserRegistered
    EventBus.subscribe(PaymentCollected, on_payment_collected)
    EventBus.subscribe(PaymentInitiated, on_payment_initiated)
    EventBus.subscribe(PaymentDisbursed, on_payment_disbursed)
    EventBus.subscribe(PaymentRefunded, on_payment_refunded)
    EventBus.subscribe(AuthOTPRequested, on_auth_otp_requested)
    EventBus.subscribe(AuthUserVerified, on_auth_user_verified)
    EventBus.subscribe(AuthUserRegistered, on_auth_user_registered)

