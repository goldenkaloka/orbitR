import logging

from domain.events.bus import EventBus
from infrastructure.notifications.service import NotificationService
from infrastructure.notifications.messages import (
    order_drafted, order_confirmed, order_preparing, order_ready,
    driver_assigned, order_picked, order_delivered, order_cancelled_customer,
    vendor_new_order, vendor_new_group_order,
)

logger = logging.getLogger(__name__)


def _notify_order_event(event):
    status_msg = f"Order status: {event.event_type}"
    from apps.events.utils import broadcast_order_status
    from apps.events.webhooks import fire_order_webhook
    logger.info(f"Broadcasting {event.event_type} for {event.aggregate_id}")

    try:
        from apps.orders.models import Order
        order = Order.objects.select_related('customer', 'restaurant__owner', 'driver__user').get(id=event.aggregate_id)

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

        broadcast_order_status(
            order_id=str(event.aggregate_id),
            status=event.event_type,
            message=status_msg,
        )

        extra_data = {"order_id": str(order.id), "type": "order_update", "status": event.event_type}

        if event.event_type == 'OrderCreated':
            if not event.payload.get('is_pool'):
                oc_title, oc_body = order_drafted(short_id)
                NotificationService.send_push(
                    user_id=str(order.customer.id),
                    title=oc_title, body=oc_body, data=extra_data,
                )
        elif event.event_type == 'OrderConfirmed':
            co_title, co_body = order_confirmed(short_id)
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title=co_title, body=co_body, data=extra_data,
            )
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
                title=v_title, body=v_body, data=vendor_data,
            )
            vendor_phone = order.restaurant.owner.phone
            vendor_name = order.restaurant.owner.username or "Vendor"
            if order.group_breakdown:
                sms_text = (
                    f"Habari {vendor_name}, una oda mpya ya KIKUNDI "
                    f"#{str(order.id).split('-')[0]}.\n"
                )
                for member in order.group_breakdown:
                    items_str = ", ".join([f"{i['quantity']}x {i['food_name']}" for i in member['items']])
                    sms_text += f"- {member['username']}: {items_str}\n"
                sms_text += "Tafadhali pakia kila mteja kivyake kwa urahisi wa usambazaji."
            else:
                sms_text = (
                    f"Habari {vendor_name}, una oda mpya "
                    f"#{str(order.id).split('-')[0]} kwenye Mwanachuoshop. "
                    f"Tafadhali iandae sasa."
                )
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
                title=msg_title, body=status_msg, data=extra_data,
            )
        elif event.event_type == 'OrderCancelled':
            cc_title, _ = order_cancelled_customer(short_id)
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title=cc_title, body=status_msg, data=extra_data,
            )
    except Exception as e:
        logger.error(f"Push notification failed for {event.event_type}: {str(e)}")

    payload = event.payload.copy()
    payload['message'] = status_msg
    payload['aggregate_id'] = str(event.aggregate_id)
    fire_order_webhook.delay(event.event_type, payload)


def on_food_ready_cross_batching(event):
    from apps.orders.models import Order
    from domain.services.batch_pipeline import BatchPipeline
    from domain.services.dispatch_tasks import dispatch_order_task

    try:
        order = Order.objects.select_related('restaurant').get(id=event.aggregate_id)
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


def on_order_delivered(event):
    logger.info(f"Received OrderDelivered event for order {event.aggregate_id}")

    from apps.orders.models import Order
    from apps.payments.models import Payment
    from domain.events.types import PaymentReleaseRequested, PoolCompleted
    from domain.fsm.pool_fsm import PoolFSM
    from django.db import transaction

    try:
        order = Order.objects.select_related('customer').get(id=event.aggregate_id)
        title, body = order_delivered(str(order.id)[:8])
        NotificationService.send_push(
            user_id=str(order.customer.id),
            title=title, body=body,
            data={"order_id": str(order.id), "type": "delivery_complete"},
        )
    except Order.DoesNotExist:
        logger.error(f"Order {event.aggregate_id} not found for delivery notification")

    try:
        if order.pool:
            member_ids = order.pool.members.values_list('id', flat=True)
            member_payments = Payment.objects.filter(payable_type='pool_member', payable_id__in=member_ids)
            for p in member_payments:
                EventBus.publish(PaymentReleaseRequested(
                    aggregate_id=p.id,
                    payload={'order_id': str(order.id)},
                ))
        else:
            payment = Payment.objects.get(payable_type='order', payable_id=event.aggregate_id)
            EventBus.publish(PaymentReleaseRequested(
                aggregate_id=payment.id,
                payload={'order_id': str(order.id)},
            ))
    except Exception as e:
        logger.error(f"Failed to trigger payment release for order {event.aggregate_id}: {str(e)}")

    if order.pool:
        try:
            pool = order.pool
            if pool.status == 'DELIVERING':
                total = pool.final_orders.count()
                delivered = pool.final_orders.filter(status='DELIVERED').count()
                if total > 0 and delivered == total:
                    pool.status = PoolFSM.transition(pool.status, 'COMPLETED')
                    pool.save(update_fields=['status'])
                    transaction.on_commit(lambda: EventBus.publish(PoolCompleted(
                        aggregate_id=pool.id,
                        payload={'order_id': str(order.id)},
                    )))
                    logger.info(f"Pool {pool.id} transitioned DELIVERING -> COMPLETED (all {total} orders delivered)")
        except Exception as e:
            logger.error(f"Failed to complete pool for order {event.aggregate_id}: {str(e)}")


def on_dispatch_failed(event):
    order_id = event.payload.get('order_id')
    attempt_retry = event.payload.get('attempt_retry', True)
    logger.info(f"Dispatch failed for order {order_id}")

    from apps.events.utils import broadcast_order_status
    broadcast_order_status(
        order_id=str(order_id),
        status='DispatchFailed',
        message="Searching for a rider..." if attempt_retry else "No riders available. Please contact support.",
    )

    NotificationService.notify_admins(
        title="Dispatch Alert",
        body=f"Order {order_id} failed to dispatch automatically.",
        data={"order_id": str(order_id), "type": "dispatch_failure"},
    )


def register_handlers():
    from domain.events.types import (
        OrderCreated, OrderConfirmed, OrderPreparing, FoodReady,
        DriverAssigned, OrderPicked, OrderDelivered, OrderCancelled,
        DispatchFailed,
    )
    order_events = [
        OrderCreated, OrderConfirmed, OrderPreparing, FoodReady,
        DriverAssigned, OrderPicked, OrderDelivered, OrderCancelled,
        DispatchFailed,
    ]
    for event_type in order_events:
        EventBus.subscribe(event_type, _notify_order_event)

    EventBus.subscribe(OrderDelivered, on_order_delivered)
    EventBus.subscribe(DispatchFailed, on_dispatch_failed)
    EventBus.subscribe(FoodReady, on_food_ready_cross_batching)
