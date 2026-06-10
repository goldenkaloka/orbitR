from domain.events.bus import EventBus
from apps.payments.models import Payment
from domain.services.payment_services import ReleasePaymentService
from infrastructure.notifications.service import NotificationService
from infrastructure.notifications.messages import (
    order_drafted, order_confirmed, order_preparing, order_ready,
    driver_assigned, order_picked, order_delivered, order_cancelled_customer,
    refund_processed, vendor_new_order, vendor_new_group_order,
    vendor_funds_received, vendor_dispute_raised, vendor_dispute_resolved,
    vendor_batch_delivering, driver_earning_credited, driver_dispute_raised,
    driver_dispute_resolved, pool_locked, pool_paid, pool_cancelled,
    pool_completed, dispute_raised_customer, dispute_resolved_customer,
    admin_pool_cancelled_batch_transit, admin_new_dispute,
)
from domain.events.types import (
    OrderDelivered, PaymentReleaseRequested, PaymentCollected,
    PaymentInitiated, PaymentDisbursed, PaymentRefunded, OrderConfirmed,
    OrderDisputed, OrderResolved,
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
        title, body = order_delivered(str(order.id)[:8])
        NotificationService.send_push(
            user_id=str(order.customer.id),
            title=title,
            body=body,
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
                payload={'order_id': str(order.id)}
            ))
    except Exception as e:
        logger.error(f"Failed to trigger payment release for order {event.aggregate_id}: {str(e)}")

    # 3. Complete pool if all its orders are delivered
    if order.pool:
        try:
            pool = order.pool
            if pool.status == 'DELIVERING':
                total = pool.final_orders.count()
                delivered = pool.final_orders.filter(status='DELIVERED').count()
                if total > 0 and delivered == total:
                    from domain.fsm.pool_fsm import PoolFSM
                    from domain.events.types import PoolCompleted
                    from django.db import transaction
                    pool.status = PoolFSM.transition(pool.status, 'COMPLETED')
                    pool.save(update_fields=['status'])
                    transaction.on_commit(lambda: EventBus.publish(PoolCompleted(
                        aggregate_id=pool.id,
                        payload={'order_id': str(order.id)}
                    )))
                    logger.info(f"Pool {pool.id} transitioned DELIVERING -> COMPLETED (all {total} orders delivered)")
        except Exception as e:
            logger.error(f"Failed to complete pool for order {event.aggregate_id}: {str(e)}")


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
    No-op: individual auto-dispatch was removed in favour of the batching
    pipeline (triggered by FoodReady). All orders reach dispatch via
    on_food_ready_cross_batching → BatchPipeline.
    """
    logger.debug(f"Order {event.aggregate_id} confirmed — skipping individual dispatch")

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
        v_title, v_body = vendor_funds_received(str(order.id)[:8])
        NotificationService.send_push(
            user_id=str(order.restaurant.owner.id),
            title=v_title,
            body=v_body,
            data={"order_id": str(order.id), "type": "payment_received"}
        )
        # Notify Driver if any
        if order.driver:
            d_title, d_body = driver_earning_credited(str(order.id)[:8])
            NotificationService.send_push(
                user_id=str(order.driver.user.id),
                title=d_title,
                body=d_body,
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
        r_title, r_body = refund_processed(str(order.id)[:8])
        NotificationService.send_push(
            user_id=str(order.customer.id),
            title=r_title,
            body=r_body,
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
        short_id = str(order.id)[:8]
        messages = {
            'OrderCreated': order_drafted(short_id)[1],
            'OrderConfirmed': order_confirmed(short_id)[1],
            'OrderPreparing': order_preparing(short_id)[1],
            'FoodReady': order_ready(short_id)[1],
            'DriverAssigned': driver_assigned(short_id)[1],
            'OrderPicked': f"{order_picked(short_id)[1]} PIN: {getattr(order, 'delivery_otp', '####')}",
            'OrderDelivered': order_delivered(short_id)[1],
            'OrderCancelled': order_cancelled_customer(short_id)[1],
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
                oc_title, oc_body = order_drafted(short_id)
                NotificationService.send_push(
                    user_id=str(order.customer.id),
                    title=oc_title,
                    body=oc_body,
                    data=extra_data
                )
        elif event.event_type == 'OrderConfirmed':
            # 1. Notify Customer
            co_title, co_body = order_confirmed(short_id)
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title=co_title,
                body=co_body,
                data=extra_data
            )
            # 2. Notify Vendor (Now only after payment) — with action buttons
            vendor_short_id = str(order.id).split('-')[0]
            is_group = bool(order.group_breakdown)
            items_count = sum(
                item.get('quantity', 0) for member in (order.group_breakdown or [])
                for item in member.get('items', [])
            ) if is_group else order.items.count()

            vendor_data = {
                    **extra_data,
                    'type': 'vendor_order',
                    'order_short_id': vendor_short_id,
                    'items_count': items_count,
                    'is_group': is_group,
                    'restaurant_name': order.restaurant.name,
                }
            v_title, v_body = (
                vendor_new_group_order(vendor_short_id, items_count)
                if is_group else vendor_new_order(vendor_short_id, items_count)
            )
            NotificationService.send_push(
                user_id=str(order.restaurant.owner.id),
                title=v_title,
                body=v_body,
                data=vendor_data,
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
            msg_title = {
                'OrderPreparing': order_preparing(short_id)[0],
                'FoodReady': order_ready(short_id)[0],
                'DriverAssigned': driver_assigned(short_id)[0],
                'OrderPicked': order_picked(short_id)[0],
            }[event.event_type]
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title=msg_title,
                body=status_msg,
                data=extra_data
            )
        elif event.event_type == 'OrderCancelled':
            cc_title, _ = order_cancelled_customer(short_id)
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title=cc_title,
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


# Batch lifecycle syncing has been removed.
# Order fulfillment states (ASSIGNED, PICKED, DELIVERED, CANCELLED) are no
# longer replicated on the batch model. Batch tracks only dispatch states



def on_pool_locked(event):
    """Notify all pool members that payment is now required."""
    pool_id = event.payload.get('pool_id')
    try:
        from apps.orders.models import OrderPool
        pool = OrderPool.objects.prefetch_related('members__user').get(id=pool_id)
        pl_title, pl_body = pool_locked(pool.restaurant.name)
        for member in pool.members.all():
            if member.user_id:
                NotificationService.send_push(
                    user_id=str(member.user_id),
                    title=pl_title,
                    body=pl_body,
                    data={'pool_id': str(pool_id), 'type': 'pool_locked'},
                )
    except Exception as e:
        logger.error(f"[PoolLocked] Failed to notify pool members for {pool_id}: {e}")


def on_pool_paid(event):
    """Notify all pool members that the group order is paid and being prepared."""
    pool_id = event.payload.get('pool_id')
    try:
        from apps.orders.models import OrderPool
        pool = OrderPool.objects.prefetch_related('members__user').get(id=pool_id)
        pp_title, pp_body = pool_paid(pool.restaurant.name)
        for member in pool.members.all():
            if member.user_id:
                NotificationService.send_push(
                    user_id=str(member.user_id),
                    title=pp_title,
                    body=pp_body,
                    data={'pool_id': str(pool_id), 'type': 'pool_paid'},
                )
    except Exception as e:
        logger.error(f"[PoolPaid] Failed to notify pool members for {pool_id}: {e}")


def on_pool_cancelled(event):
    """
    Handle PoolCancelled event.
    Notify all pool members and, if a batch was dispatched, alert admins.
    """
    pool_id = event.payload.get('pool_id')
    batch_id = event.payload.get('batch_id')
    actor_id = event.payload.get('actor_id')

    # Notify all pool members
    try:
        from apps.orders.models import OrderPool
        pool = OrderPool.objects.prefetch_related('members__user').get(id=pool_id)
        pc_title, pc_body = pool_cancelled(pool.restaurant.name)
        for member in pool.members.all():
            if member.user_id and str(member.user_id) != actor_id:
                NotificationService.send_push(
                    user_id=str(member.user_id),
                    title=pc_title,
                    body=pc_body,
                    data={'pool_id': str(pool_id), 'type': 'pool_cancelled'},
                )
    except Exception as e:
        logger.error(f"[PoolCancelled] Failed to notify pool members for {pool_id}: {e}")

    # If batch was already dispatched, alert admins
    if not batch_id:
        return

    from apps.orders.models import DeliveryBatch
    try:
        batch = DeliveryBatch.objects.get(id=batch_id)
        active_orders = batch.orders.filter(status__in=['ASSIGNED', 'PICKED']).exists()
        if active_orders:
            logger.warning(
                f"[PoolCancelled] Pool {pool_id} cancelled but batch {batch_id} "
                f"has active orders — manual intervention required."
            )
            apt_title, apt_body = admin_pool_cancelled_batch_transit(
                str(pool_id)[:8], str(batch_id)[:8], batch.status
            )
            NotificationService.notify_admins(
                title=apt_title,
                body=apt_body,
                data={
                    'pool_id': str(pool_id),
                    'batch_id': str(batch_id),
                    'type': 'pool_cancel_late',
                },
            )
    except DeliveryBatch.DoesNotExist:
        pass


def on_pool_completed(event):
    """Notify all pool members that all deliveries are complete."""
    pool_id = event.payload.get('pool_id')
    try:
        from apps.orders.models import OrderPool
        pool = OrderPool.objects.prefetch_related('members__user').get(id=pool_id)
        pco_title, pco_body = pool_completed(pool.restaurant.name)
        for member in pool.members.all():
            if member.user_id:
                NotificationService.send_push(
                    user_id=str(member.user_id),
                    title=pco_title,
                    body=pco_body,
                    data={'pool_id': str(pool_id), 'type': 'pool_completed'},
                )
    except Exception as e:
        logger.error(f"[PoolCompleted] Failed to notify pool members for {pool_id}: {e}")


def on_order_disputed(event):
    """Handle dispute raised — notify customer, vendor, and admin."""
    from apps.events.utils import broadcast_order_status
    from apps.orders.models import Order

    order_id = event.payload.get('order_id')
    try:
        order = Order.objects.select_related('customer', 'restaurant__owner', 'driver__user').get(id=order_id)
        broadcast_order_status(
            order_id=order_id,
            status='DISPUTED',
            message="A dispute has been raised on this order",
        )

        dc_title, dc_body = dispute_raised_customer()
        NotificationService.send_push(
            user_id=str(order.customer.id),
            title=dc_title,
            body=dc_body,
            data={"order_id": str(order.id), "type": "dispute_raised"},
        )
        vd_title, vd_body = vendor_dispute_raised(str(order.id)[:8])
        NotificationService.send_push(
            user_id=str(order.restaurant.owner.id),
            title=vd_title,
            body=vd_body,
            data={"order_id": str(order.id), "type": "dispute_raised"},
        )
        if order.driver and order.driver.user:
            dd_title, dd_body = driver_dispute_raised(str(order.id)[:8])
            NotificationService.send_push(
                user_id=str(order.driver.user.id),
                title=dd_title,
                body=dd_body,
                data={"order_id": str(order.id), "type": "dispute_raised"},
            )

        ad_title, ad_body = admin_new_dispute(str(order.id)[:8], order.customer.username)
        NotificationService.notify_admins(
            title=ad_title,
            body=ad_body,
            data={"order_id": str(order_id), "type": "new_dispute"},
        )
    except Order.DoesNotExist:
        logger.error(f"[DisputeHandler] Order {order_id} not found")


def on_order_resolved(event):
    """Handle dispute resolved — notify all parties."""
    from apps.events.utils import broadcast_order_status
    from apps.orders.models import Order

    order_id = event.payload.get('order_id')
    resolution = event.payload.get('resolution', '')
    try:
        order = Order.objects.select_related('customer', 'restaurant__owner', 'driver__user').get(id=order_id)
        broadcast_order_status(
            order_id=order_id,
            status='RESOLVED',
            message=f"Dispute resolved: {resolution}",
        )

        drc_title, drc_body = dispute_resolved_customer(
            resolution.replace('RESOLVED_', '').replace('_', ' ').title()
        )
        NotificationService.send_push(
            user_id=str(order.customer.id),
            title=drc_title,
            body=drc_body,
            data={"order_id": str(order.id), "type": "dispute_resolved"},
        )
        vdr_title, vdr_body = vendor_dispute_resolved(str(order.id)[:8])
        NotificationService.send_push(
            user_id=str(order.restaurant.owner.id),
            title=vdr_title,
            body=vdr_body,
            data={"order_id": str(order.id), "type": "dispute_resolved"},
        )
    except Order.DoesNotExist:
        logger.error(f"[ResolveHandler] Order {order_id} not found")


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
    
    # Batch lifecycle syncing via events has been removed.
    # Batch only tracks dispatch states. Order fulfillment is independent.

    # Cross-Restaurant Batching
    from domain.events.types import FoodReady
    EventBus.subscribe(FoodReady, on_food_ready_cross_batching)

    # Dispute Events
    EventBus.subscribe(OrderDisputed, on_order_disputed)
    EventBus.subscribe(OrderResolved, on_order_resolved)

    # Pool lifecycle events
    from domain.events.types import PoolCancelled, PoolReady, PoolLocked, PoolPaid, PoolCompleted
    EventBus.subscribe(PoolCancelled, on_pool_cancelled)
    EventBus.subscribe(PoolReady, on_pool_ready)
    EventBus.subscribe(PoolLocked, on_pool_locked)
    EventBus.subscribe(PoolPaid, on_pool_paid)
    EventBus.subscribe(PoolCompleted, on_pool_completed)

    # Other Domain Events
    from domain.events.types import PaymentCollected, AuthOTPRequested, AuthUserVerified, AuthUserRegistered
    EventBus.subscribe(PaymentCollected, on_payment_collected)
    EventBus.subscribe(PaymentInitiated, on_payment_initiated)
    EventBus.subscribe(PaymentDisbursed, on_payment_disbursed)
    EventBus.subscribe(PaymentRefunded, on_payment_refunded)
    EventBus.subscribe(AuthOTPRequested, on_auth_otp_requested)
    EventBus.subscribe(AuthUserVerified, on_auth_user_verified)
    EventBus.subscribe(AuthUserRegistered, on_auth_user_registered)

