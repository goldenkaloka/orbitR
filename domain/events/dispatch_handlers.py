"""
Dispatch event handlers.

A batch in PENDING_DRIVER needs someone to allocate a driver. Who is notified
is decided by Restaurant.dispatch_mode, carried on the event payload:

- admin       — admin dispatch queue. Admin gets SMS + push (billable, so the
                upstream offer_sent_at throttle matters) plus a realtime push to
                any open dashboard.
- restaurant  — the restaurant's own delivery tab. Push to the owner and the
                stall staff whose sections are in the batch.

The event itself stays neutral ("a driver allocation is needed"); who must act
is derived policy, applied here rather than baked into the event name.
"""

import logging

logger = logging.getLogger(__name__)


def on_driver_dispatch_requested(event):
    """Notify whoever owns dispatch for this batch."""
    from apps.events.utils import broadcast_to_admins

    batch_id = str(event.aggregate_id)
    payload = event.payload or {}
    dispatch_mode = payload.get('dispatch_mode') or 'admin'

    if dispatch_mode == 'restaurant':
        _notify_restaurant(batch_id, payload)
    else:
        _notify_admins(batch_id, payload)
        broadcast_to_admins(_realtime_payload(batch_id, payload, dispatch_mode))


def _realtime_payload(batch_id, payload, dispatch_mode):
    return {
        'type': 'dispatch_requested',
        'batch_id': batch_id,
        'order_count': payload.get('order_count') or 0,
        'restaurant_ids': [str(r) for r in (payload.get('restaurant_ids') or [])],
        'restaurant_name': payload.get('restaurant_name') or 'restauranti',
        'waiting_minutes': payload.get('waiting_minutes') or 0,
        'is_reminder': bool(payload.get('is_reminder')),
        'dispatch_mode': dispatch_mode,
    }


def _notify_admins(batch_id, payload):
    from infrastructure.notifications.messages import admin_batch_needs_driver
    from infrastructure.notifications.service import NotificationService

    title, body = admin_batch_needs_driver(
        order_count=payload.get('order_count') or 0,
        restaurant_name=payload.get('restaurant_name') or 'restauranti',
        waiting_minutes=payload.get('waiting_minutes') or 0,
        is_reminder=bool(payload.get('is_reminder')),
    )

    try:
        NotificationService.notify_admins(
            title=title,
            body=body,
            data=_realtime_payload(batch_id, payload, 'admin'),
            sms=True,
        )
    except Exception as exc:
        logger.error(f"[DispatchHandler] Admin notification failed for batch {batch_id}: {exc}")


def _notify_restaurant(batch_id, payload):
    """Push to the restaurant owner and the stall staff with sections in batch."""
    from apps.orders.models import DeliveryBatch
    from apps.restaurants.models import Restaurant, SectionStaff
    from infrastructure.notifications.channels import default_channel
    from infrastructure.notifications.messages import vendor_need_driver

    order_count = payload.get('order_count') or 0
    title, body = vendor_need_driver(order_count)

    try:
        batch = DeliveryBatch.objects.get(id=batch_id)
    except DeliveryBatch.DoesNotExist:
        logger.warning(f"[DispatchHandler] Batch {batch_id} not found for vendor notify")
        return

    restaurant_ids = payload.get('restaurant_ids') or []
    if not restaurant_ids:
        restaurant_ids = list(
            batch.orders.values_list('restaurant_id', flat=True).distinct()
        )

    data = {
        'type': 'batch_needs_driver',
        'batch_id': batch_id,
        'order_count': order_count,
        'dispatch_mode': 'restaurant',
    }

    for restaurant in Restaurant.objects.filter(id__in=restaurant_ids):
        if restaurant.owner_id:
            default_channel.send_push(
                user_id=str(restaurant.owner_id),
                title=title,
                body=body,
                data={**data, 'restaurant_id': str(restaurant.id)},
            )

        section_ids = batch.section_statuses.filter(
            section__restaurant_id=restaurant.id
        ).values_list('section_id', flat=True)
        for staff in SectionStaff.objects.filter(section_id__in=list(section_ids)):
            default_channel.send_push(
                user_id=str(staff.user_id),
                title=title,
                body=body,
                data={**data, 'restaurant_id': str(restaurant.id)},
            )


def register_handlers():
    from domain.events.bus import EventBus
    from domain.events.types import DriverDispatchRequested

    EventBus.subscribe(DriverDispatchRequested, on_driver_dispatch_requested)