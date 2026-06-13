import logging

from domain.events.bus import EventBus
from domain.events.types import OrderDisputed, OrderResolved
from infrastructure.notifications.service import NotificationService
from infrastructure.notifications.messages import (
    dispute_raised_customer, vendor_dispute_raised, driver_dispute_raised,
    admin_new_dispute, dispute_resolved_customer, vendor_dispute_resolved,
)

logger = logging.getLogger(__name__)


def on_order_disputed(event):
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
            title=dc_title, body=dc_body,
            data={"order_id": str(order.id), "type": "dispute_raised"},
        )
        vd_title, vd_body = vendor_dispute_raised(str(order.id)[:8])
        NotificationService.send_push(
            user_id=str(order.restaurant.owner.id),
            title=vd_title, body=vd_body,
            data={"order_id": str(order.id), "type": "dispute_raised"},
        )
        if order.driver and order.driver.user:
            dd_title, dd_body = driver_dispute_raised(str(order.id)[:8])
            NotificationService.send_push(
                user_id=str(order.driver.user.id),
                title=dd_title, body=dd_body,
                data={"order_id": str(order.id), "type": "dispute_raised"},
            )

        ad_title, ad_body = admin_new_dispute(str(order.id)[:8], order.customer.username)
        NotificationService.notify_admins(
            title=ad_title, body=ad_body,
            data={"order_id": str(order_id), "type": "new_dispute"},
        )
    except Order.DoesNotExist:
        logger.error(f"[DisputeHandler] Order {order_id} not found")


def on_order_resolved(event):
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
            title=drc_title, body=drc_body,
            data={"order_id": str(order.id), "type": "dispute_resolved"},
        )
        vdr_title, vdr_body = vendor_dispute_resolved(str(order.id)[:8])
        NotificationService.send_push(
            user_id=str(order.restaurant.owner.id),
            title=vdr_title, body=vdr_body,
            data={"order_id": str(order.id), "type": "dispute_resolved"},
        )
    except Order.DoesNotExist:
        logger.error(f"[ResolveHandler] Order {order_id} not found")


def register_handlers():
    EventBus.subscribe(OrderDisputed, on_order_disputed)
    EventBus.subscribe(OrderResolved, on_order_resolved)
