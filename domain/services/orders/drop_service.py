"""Scheduled drops: many orders, one trip, one gate.

A drop window is a recurring hand-over slot at a shared point (a hostel gate,
a campus block, an office lobby). Customers order against the window instead of
against their own address, which is what makes the trip cheap: one rider, one
route, eight bags.

This module owns the calendar rules — when a window next runs, when it closes,
how full it is — so placement, dispatch and the cut-off job all agree.
"""

import logging
from datetime import datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.orders.models import DropPoint, DropWindow, Order, OrderPool

logger = logging.getLogger(__name__)

# Statuses that no longer occupy a seat in a drop window.
DEAD_ORDER_STATUSES = ('CANCELLED', 'EXPIRED', 'REFUNDED')

# Pools that have not spawned their final orders yet still hold seats.
OPEN_POOL_STATUSES = ('OPEN', 'LOCKED')


class DropUnavailable(Exception):
    """This drop window cannot take the order (closed, full, or not running)."""


def local_now(now=None):
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, dt_timezone.utc)
    return now.astimezone(_business_tz())


def _business_tz():
    """Drop windows are written in the market's clock, not UTC."""
    return ZoneInfo(getattr(settings, 'PREORDER_TIMEZONE', 'Africa/Dar_es_Salaam'))


def _aware(on_date, at):
    return timezone.make_aware(
        datetime.combine(on_date, at), _business_tz(),
    )


def window_start(window, on_date):
    """The moment the hand-over window opens on ``on_date``."""
    return _aware(on_date, window.start_time)


def window_end(window, on_date):
    return _aware(on_date, window.end_time)


def cutoff_at(window, on_date):
    """The last moment an order may join the window on ``on_date``."""
    return _aware(on_date, window.cutoff_time)


def next_occurrence(window, now=None):
    """The next time this window opens, or None if it never does.

    A window that started earlier today is not "next": its cut-off has already
    passed, so the answer rolls to the next matching day.
    """
    now = local_now(now)
    for offset in range(0, 9):
        day = now.date() + timedelta(days=offset)
        if window.weekday is not None and day.weekday() != window.weekday:
            continue
        start = window_start(window, day)
        if start > now:
            return start
    return None


def occurrences(window, days=2, now=None):
    """Every start time for this window inside the next ``days`` days."""
    now = local_now(now)
    found = []
    for offset in range(0, max(days, 1) + 1):
        day = now.date() + timedelta(days=offset)
        if window.weekday is not None and day.weekday() != window.weekday:
            continue
        start = window_start(window, day)
        if start <= now:
            continue
        if start > now + timedelta(days=days):
            continue
        found.append(start)
    return found


def occurrence_range(window, on_date):
    """``(start, end)`` of one occurrence, in the market's clock.

    Matching on this range rather than ``scheduled_for__date`` keeps early
    windows correct: a 00:30 hand-over is still the previous UTC date, so a
    date filter would count zero seats and never fill the window.
    """
    return window_start(window, on_date), window_end(window, on_date)


def reserved_seats(window, on_date):
    """Seats already taken for one occurrence: orders plus open pools."""
    start, end = occurrence_range(window, on_date)
    in_occurrence = Q(scheduled_for__gte=start, scheduled_for__lt=end)

    orders = (
        Order.objects.filter(in_occurrence, drop_window=window)
        .exclude(status__in=DEAD_ORDER_STATUSES)
        .count()
    )

    pools = (
        OrderPool.objects.filter(
            in_occurrence,
            drop_window=window,
            status__in=OPEN_POOL_STATUSES,
        )
        .annotate(member_count=Count('members'))
        .aggregate(total=Sum('member_count'))
    )
    return orders + (pools['total'] or 0)


def capacity(window):
    return window.max_orders or window.drop_point.max_orders


def window_state(window, start, now=None):
    """``(bookable, reason)`` for one occurrence of a window."""
    now = local_now(now)
    if not window.is_active or not window.drop_point.is_active:
        return False, 'This drop point is not taking orders.'
    if now >= cutoff_at(window, start.date()):
        cutoff = window.cutoff_time.strftime('%H:%M')
        return False, f'Orders for this drop closed at {cutoff}.'
    if reserved_seats(window, start.date()) >= capacity(window):
        return False, 'This drop is full — try the next window.'
    return True, ''


def validate_drop_booking(window, scheduled_for=None, now=None, restaurant=None):
    """Resolve and check the occurrence a customer is booking.

    Returns the window's start datetime. Raises :class:`DropUnavailable` with a
    message that can be shown to the customer as-is.

    ``restaurant`` enforces the one-kitchen rule: a drop trip is one rider, one
    pickup, one gate. A window served by two kitchens becomes two trips that
    each pay the flat trip fee against the same fees.
    """
    now = local_now(now)
    if not window.is_active or not window.drop_point.is_active:
        raise DropUnavailable('This drop point is not taking orders.')

    if scheduled_for is not None:
        if timezone.is_naive(scheduled_for):
            scheduled_for = timezone.make_aware(
                scheduled_for, _business_tz(),
            )
        start = window_start(window, scheduled_for.astimezone(_business_tz()).date())
        if window.weekday is not None and start.weekday() != window.weekday:
            raise DropUnavailable(
                f'{window.label} runs on {window.get_weekday_display()} only.'
            )
        if start > now + timedelta(days=14):
            raise DropUnavailable('This drop can only be booked 14 days ahead.')
    else:
        start = next_occurrence(window, now)
        if start is None:
            raise DropUnavailable('This drop is not scheduled for any upcoming day.')

    bookable, reason = window_state(window, start, now)
    if not bookable:
        raise DropUnavailable(reason)

    if restaurant is not None:
        taken_by = other_restaurant_in_occurrence(window, start, restaurant)
        if taken_by:
            raise DropUnavailable(
                f'This drop is being served by {taken_by} — try another window.'
            )
    return start


def other_restaurant_in_occurrence(window, start, restaurant):
    """Name of a kitchen already holding this occurrence, if not ``restaurant``."""
    start_at, end_at = occurrence_range(window, start.date())
    in_occurrence = Q(scheduled_for__gte=start_at, scheduled_for__lt=end_at)

    order_restaurant = (
        Order.objects.filter(in_occurrence, drop_window=window)
        .exclude(status__in=DEAD_ORDER_STATUSES)
        .exclude(restaurant=restaurant)
        .values_list('restaurant__name', flat=True)
        .first()
    )
    if order_restaurant:
        return order_restaurant

    return (
        OrderPool.objects.filter(
            in_occurrence,
            drop_window=window,
            status__in=OPEN_POOL_STATUSES,
        )
        .exclude(restaurant=restaurant)
        .values_list('restaurant__name', flat=True)
        .first()
    )


def _occupant(window, start):
    """Who already holds one occurrence: ``(restaurant_id, name, pool_id)``.

    A drop trip is one kitchen, one pickup, one gate, so the app needs to know
    which kitchen claimed the window before another customer tries to join
    with a different menu.
    """
    start_at, end_at = occurrence_range(window, start.date())
    in_occurrence = Q(scheduled_for__gte=start_at, scheduled_for__lt=end_at)

    order = (
        Order.objects.filter(in_occurrence, drop_window=window)
        .exclude(status__in=DEAD_ORDER_STATUSES)
        .values('restaurant_id', 'restaurant__name')
        .first()
    )
    if order:
        return order['restaurant_id'], order['restaurant__name'], None

    pool = (
        OrderPool.objects.filter(
            in_occurrence,
            drop_window=window,
            status__in=OPEN_POOL_STATUSES,
        )
        .values('id', 'restaurant_id', 'restaurant__name')
        .first()
    )
    if pool:
        return pool['restaurant_id'], pool['restaurant__name'], pool['id']
    return None, None, None


def describe_window(window, start, now=None):
    """A dict the API can hand straight to the app."""
    from domain.services.pricing_service import PricingService

    now = local_now(now)
    bookable, reason = window_state(window, start, now)
    point = window.drop_point
    serving_id, serving_name, open_pool_id = _occupant(window, start)
    # The flat fee is the window's; the service fee comes from the policy so
    # the app can quote and submit the exact total without hardcoding it.
    policy = PricingService.get_active_policy()
    service_fee = float(policy.service_fee) if policy else 300.0
    return {
        'id': str(window.id),
        'label': window.label,
        'drop_point_id': str(point.id),
        'drop_point_name': point.name,
        'drop_point_address': point.description,
        'latitude': point.geo_location.y if point.geo_location else None,
        'longitude': point.geo_location.x if point.geo_location else None,
        'date': start.date().isoformat(),
        'start': start.isoformat(),
        'end': window_end(window, start.date()).isoformat(),
        'cutoff': cutoff_at(window, start.date()).isoformat(),
        'cutoff_label': window.cutoff_time.strftime('%H:%M'),
        'start_label': window.start_time.strftime('%H:%M'),
        'end_label': window.end_time.strftime('%H:%M'),
        'delivery_fee': float(window.delivery_fee),
        'service_fee': service_fee,
        'capacity': capacity(window),
        'remaining': max(capacity(window) - reserved_seats(window, start.date()), 0),
        'min_orders': point.min_orders,
        'serving_restaurant_id': str(serving_id) if serving_id else None,
        'serving_restaurant_name': serving_name,
        'open_pool_id': str(open_pool_id) if open_pool_id else None,
        'available': bookable,
        'unavailable_reason': reason,
    }


def open_windows(point=None, days=2, now=None):
    """Every bookable occurrence in the next ``days`` days, soonest first."""
    now = local_now(now)
    windows = DropWindow.objects.filter(
        is_active=True, drop_point__is_active=True,
    ).select_related('drop_point')
    if point is not None:
        windows = windows.filter(drop_point=point)

    described = []
    for window in windows:
        for start in occurrences(window, days=days, now=now):
            described.append(describe_window(window, start, now))
    described.sort(key=lambda item: item['start'])
    return described


def nearest_points(latitude, longitude, limit=10, max_km=15.0):
    """Active drop points near a coordinate, closest first."""
    from apps.core.geo_utils import haversine_distance

    if latitude is None or longitude is None:
        return list(DropPoint.objects.filter(is_active=True)[:limit])

    from django.contrib.gis.geos import Point

    here = Point(float(longitude), float(latitude))
    scored = []
    for point in DropPoint.objects.filter(is_active=True):
        if not point.geo_location:
            continue
        try:
            distance = haversine_distance(here, point.geo_location) / 1000.0
        except Exception:  # pragma: no cover - defensive, bad geometry row
            continue
        if distance <= max_km:
            scored.append((distance, point))
    scored.sort(key=lambda pair: pair[0])
    return [point for _distance, point in scored[:limit]]


def apply_low_fill_policy(window, on_date, now=None):
    """Decide what to do with a window that did not reach ``min_orders``.

    Called at cut-off, before the kitchen starts. Returns one of
    ``('OK', 'RUN', 'ROLLED', 'CANCELLED')`` so the caller can log what happened.

    * ``RUN``     — the trip goes out anyway; the platform absorbs the gap.
    * ``ROLL``    — orders move to the window's next occurrence, no refund, no
                    re-payment; the customer is told the new time.
    * ``CANCEL``  — every order is cancelled and refunded.
    """
    from domain.services.pricing_service import PricingService

    now = local_now(now)
    count = reserved_seats(window, on_date)
    if count >= window.drop_point.min_orders:
        return 'OK'
    if count == 0:
        return 'OK'

    policy = PricingService.get_active_policy()
    action = (getattr(policy, 'drop_low_fill_policy', None) or 'ROLL') if policy else 'ROLL'

    if action == 'RUN':
        logger.info(
            "[Drop] %s on %s has %s order(s) below the %s minimum — running anyway.",
            window, on_date, count, window.drop_point.min_orders,
        )
        return 'RUN'

    if action == 'ROLL':
        target = _next_window_after(window, on_date, seats=count)
        if target is None:
            action = 'CANCEL'
        else:
            _roll_orders(window, on_date, target)
            return 'ROLLED'

    if action == 'CANCEL':
        _cancel_orders(window, on_date)
        return 'CANCELLED'

    return 'RUN'


def _next_window_after(window, on_date, seats=0):
    """The next occurrence of this window that can still take ``seats`` orders.

    Rolling into a full window would create a trip larger than one rider can
    carry, so a full run is skipped rather than oversold.
    """
    probe = _aware(on_date, time(23, 59))
    for _ in range(8):
        candidate = next_occurrence(window, now=probe)
        if candidate is None:
            return None
        if reserved_seats(window, candidate.date()) + seats <= capacity(window):
            return candidate
        probe = candidate
    return None


def _roll_orders(window, on_date, target_start):
    """Move every order on ``on_date`` to the window's next occurrence."""
    from domain.services.orders.scheduled_orders import release_at_for

    start_at, end_at = occurrence_range(window, on_date)
    orders = list(
        Order.objects.filter(
            drop_window=window,
            scheduled_for__gte=start_at, scheduled_for__lt=end_at,
        ).exclude(status__in=DEAD_ORDER_STATUSES).select_related('restaurant')
    )

    for order in orders:
        order.scheduled_for = target_start
        if order.restaurant_id:
            order.release_at = release_at_for(order.restaurant, target_start)
        order.save(update_fields=['scheduled_for', 'release_at', 'updated_at'])
        logger.info(
            "[Drop] Rolled order %s from %s to %s.", order.id, on_date, target_start,
        )
        try:
            from infrastructure.notifications.service import NotificationService

            NotificationService.send_push(
                user_id=str(order.customer_id),
                title='Drop moved to the next window',
                body=(
                    f'Not enough orders for {window.label.lower()} at '
                    f'{window.drop_point.name}. Your order moved to '
                    f'{target_start:%a %d %b, %H:%M} — no extra charge.'
                ),
                data={
                    'order_id': str(order.id),
                    'type': 'drop_rolled',
                },
            )
        except Exception as exc:  # notification must never block the roll
            logger.warning("[Drop] Could not notify order %s: %s", order.id, exc)

    _move_batches_to_target(orders, target_start)

    OrderPool.objects.filter(
        drop_window=window,
        scheduled_for__gte=start_at, scheduled_for__lt=end_at,
        status__in=OPEN_POOL_STATUSES,
    ).update(
        scheduled_for=target_start,
        expires_at=cutoff_at(window, target_start.date()),
        updated_at=timezone.now(),
    )


def _move_batches_to_target(orders, target_start):
    """Re-anchor the pre-order batch of rolled orders to the new slot.

    Pre-orders are grouped into a slot batch when they are confirmed. Moving
    only ``scheduled_for`` would leave that batch anchored to the old time, and
    it would close and dispatch a week early.
    """
    import math

    from apps.orders.models import DeliveryBatch
    from domain.services.batch_pipeline import (
        ACCUMULATE_WINDOW_MINUTES,
        PREORDER_BATCH_BUFFER_MINUTES,
    )
    from domain.services.orders.scheduled_orders import (
        release_at_for,
        slot_window_for,
    )

    rolled_ids = {order.id for order in orders if order.batch_id}
    batch_ids = {order.batch_id for order in orders if order.batch_id}
    if not batch_ids:
        return

    now = timezone.now()
    for batch in DeliveryBatch.objects.filter(id__in=batch_ids).select_related('restaurant'):
        member_ids = set(batch.orders.values_list('id', flat=True))
        if not member_ids <= rolled_ids:
            continue
        if batch.status != 'ACCUMULATING':
            continue

        new_slot, slot_end = slot_window_for(target_start)
        last_release = release_at_for(batch.restaurant, slot_end)
        batch.preorder_slot_start = new_slot
        batch.accumulating_since = now
        batch.accumulate_window_minutes = max(
            ACCUMULATE_WINDOW_MINUTES,
            math.ceil((last_release - now).total_seconds() / 60)
            + PREORDER_BATCH_BUFFER_MINUTES,
        )
        batch.save(update_fields=[
            'preorder_slot_start', 'accumulating_since',
            'accumulate_window_minutes', 'updated_at',
        ])
        logger.info(
            "[Drop] Re-anchored drop batch %s to slot %s.", batch.id, new_slot,
        )


def _cancel_orders(window, on_date):
    """Cancel and refund every order in a window that is not running."""
    from domain.services.orders.order_cancellation import CancelOrderService
    from domain.services.payment_services import get_platform_user

    start_at, end_at = occurrence_range(window, on_date)
    actor = get_platform_user()
    orders = Order.objects.filter(
        drop_window=window,
        scheduled_for__gte=start_at, scheduled_for__lt=end_at,
    ).exclude(status__in=DEAD_ORDER_STATUSES)

    for order in orders:
        try:
            CancelOrderService.execute(
                order.id,
                actor,
                reason='Not enough orders for this drop — refunded in full.',
                category='system_timeout',
                fault='SYSTEM',
                affects_restaurant_reputation=False,
            )
        except Exception as exc:  # pragma: no cover - one bad order must not stop the rest
            logger.error("[Drop] Could not cancel order %s: %s", order.id, exc)
