import logging
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from apps.orders.models import Order, OrderPool
from apps.restaurants.models import MealPeriod
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from domain.fsm.order_fsm import OrderFSM

logger = logging.getLogger(__name__)

DEFAULT_PREP_MINUTES = 20

# Pre-orders reserve a slot from the moment the order row is created: an
# unpaid cart holds it until cleanup_unpaid_orders cancels it 30 minutes later.
SLOT_COUNTED_STATUSES = (
    'PENDING_PAYMENT',
    'SCHEDULED',
    'CREATED',
    'AWAITING_CONFIRMATION',
    'CONFIRMED',
    'PREPARING',
    'READY',
    'ASSIGNED',
    'PICKED',
)

# A bucket holds its seat from creation until it is finished or aborted; the
# member orders it spawns are excluded from the order count to avoid counting
# the same group twice.
SLOT_COUNTED_POOL_STATUSES = (
    'OPEN',
    'LOCKED',
    'PAID',
    'READY',
    'DISPATCHING',
    'DELIVERING',
)


class PreorderSlotUnavailable(ValueError):
    """The requested eating time has no remaining kitchen capacity."""


MEAL_LABELS = {
    MealPeriod.BREAKFAST: 'Breakfast',
    MealPeriod.LUNCH: 'Lunch',
    MealPeriod.DINNER: 'Dinner',
}


@dataclass(frozen=True)
class MealWindow:
    """A serving window pre-orders can be booked into."""

    kind: str
    label: str
    start: time
    end: time
    slot_capacity: int | None = None
    # Latest time of day a pre-order for this meal can be placed. None keeps
    # the old behaviour: bookable right up to the end of the window.
    preorder_cutoff: time | None = None


# Used until a restaurant configures its own serving windows.
DEFAULT_MEAL_WINDOWS = (
    MealWindow(MealPeriod.BREAKFAST, MEAL_LABELS[MealPeriod.BREAKFAST], time(7, 0), time(10, 30)),
    MealWindow(MealPeriod.LUNCH, MEAL_LABELS[MealPeriod.LUNCH], time(12, 0), time(15, 30)),
    MealWindow(MealPeriod.DINNER, MEAL_LABELS[MealPeriod.DINNER], time(18, 0), time(22, 0)),
)


def meal_windows_for(restaurant):
    """
    The restaurant's bookable meal windows, clipped to service hours.

    A restaurant that has configured any meal periods owns its schedule: its
    active rows replace the defaults, so deactivating every row stops
    pre-orders entirely. No rows at all means the platform defaults apply.
    """
    configured = list(restaurant.meal_periods.all())
    if configured:
        windows = [
            MealWindow(
                kind=row.kind,
                label=row.label or MEAL_LABELS.get(row.kind, row.kind.title()),
                start=row.start_time,
                end=row.end_time,
                slot_capacity=row.slot_capacity,
                preorder_cutoff=row.preorder_cutoff_time,
            )
            for row in configured
            if row.is_active
        ]
    else:
        windows = list(DEFAULT_MEAL_WINDOWS)

    service_start = time(hour=settings.PREORDER_SERVICE_START_HOUR)
    service_end = time(hour=settings.PREORDER_SERVICE_END_HOUR)
    effective = []
    for window in windows:
        start = max(window.start, service_start)
        end = min(window.end, service_end)
        if start < end:
            effective.append(replace(window, start=start, end=end))
    return sorted(effective, key=lambda window: window.start)


def prep_minutes_for(restaurant):
    """
    Minutes the kitchen needs before the eating time.

    A meal is only ready when every section that touches it has finished, so
    the slowest active section gates the whole order. Falling back to the
    restaurant's average keeps restaurants without section timings working.
    """
    sections = [
        section
        for section in restaurant.sections.all()
        if getattr(section, 'is_active', True)
    ]
    section_prep = [
        int(section.prep_time)
        for section in sections
        if getattr(section, 'prep_time', None)
    ]
    if section_prep:
        return max(section_prep)

    return int(
        getattr(restaurant, 'average_prep_time', None)
        or getattr(restaurant, 'prep_time', None)
        or DEFAULT_PREP_MINUTES
    )


def release_at_for(restaurant, scheduled_for):
    """When the order must enter the live kitchen pipeline."""
    return scheduled_for - timedelta(minutes=prep_minutes_for(restaurant))


def format_eating_time(scheduled_for):
    """Human-readable eating time in the pre-order timezone."""
    if not scheduled_for:
        return ''
    local = scheduled_for.astimezone(_preorder_timezone())
    return local.strftime('%a %d %b, %H:%M')


def validate_scheduled_for(scheduled_for, now=None, restaurant=None):
    """
    Check the booking is far enough ahead for the kitchen to prep it.

    ``restaurant`` is optional for backwards compatibility, but callers that
    have it should pass it: the lead window has to include the slowest
    section's prep time, otherwise a booking can be accepted whose release
    time is already in the past.
    """
    if scheduled_for is None:
        return None

    now = now or timezone.now()
    min_lead = settings.PREORDER_MIN_LEAD_MINUTES
    max_horizon = settings.PREORDER_MAX_HORIZON_DAYS

    prep_minutes = prep_minutes_for(restaurant) if restaurant is not None else 0
    lead_minutes = min_lead + prep_minutes

    if scheduled_for < now + timedelta(minutes=lead_minutes):
        detail = (
            f" (including {prep_minutes} minutes of prep time)"
            if restaurant is not None
            else ''
        )
        raise ValueError(
            f"Pre-orders must be booked at least {lead_minutes} minutes ahead{detail}."
        )
    if scheduled_for > now + timedelta(days=max_horizon):
        raise ValueError(
            f"Pre-orders can be booked at most {max_horizon} days ahead."
        )
    return scheduled_for


def slot_window_for(scheduled_for):
    """Half-open [start, end) eating-time slot containing the booking."""
    slot_minutes = settings.PREORDER_SLOT_MINUTES
    epoch_minutes = int(scheduled_for.timestamp()) // 60
    start_minutes = (epoch_minutes // slot_minutes) * slot_minutes
    start = datetime.fromtimestamp(start_minutes * 60, tz=scheduled_for.tzinfo)
    return start, start + timedelta(minutes=slot_minutes)


def slot_capacity_for(restaurant):
    """
    Pre-orders accepted per slot. An explicit restaurant override wins;
    otherwise the limit comes from how many orders the kitchen can start
    during one slot, based on each section's concurrency and prep time.
    """
    explicit = restaurant.preorder_slot_capacity
    if explicit is not None:
        return explicit

    slot_minutes = settings.PREORDER_SLOT_MINUTES
    # The feed asks this for every kitchen, so read the prefetched rows when
    # they exist instead of firing one query per restaurant.
    prefetched = getattr(restaurant, '_prefetched_objects_cache', {}).get('sections')
    if prefetched is not None:
        sections = [section for section in prefetched if section.is_active]
    else:
        sections = list(restaurant.sections.filter(is_active=True))
    if not sections:
        return 1

    capacity = 0
    for section in sections:
        prep = max(int(section.prep_time or DEFAULT_PREP_MINUTES), 1)
        per_slot = (section.max_concurrent_orders * slot_minutes + prep - 1) // prep
        capacity += max(per_slot, 1)
    return capacity


def _preorder_timezone():
    return ZoneInfo(getattr(settings, 'PREORDER_TIMEZONE', 'Africa/Dar_es_Salaam'))


def meal_window_for(restaurant, scheduled_for):
    """The serving window containing the booking, or None outside all of them."""
    local_time = scheduled_for.astimezone(_preorder_timezone()).time()
    for window in meal_windows_for(restaurant):
        if window.start <= local_time < window.end:
            return window
    return None


def cutoff_for_meal(restaurant, meal_kind):
    """The configured pre-order cutoff for one meal, if any."""
    for window in meal_windows_for(restaurant):
        if window.kind == meal_kind:
            return window.preorder_cutoff
    return None


def meal_cutoff_passed(window, scheduled_for, now=None):
    """
    Whether the ordering deadline for ``window`` has passed for that day.

    The cutoff is a *placing* deadline, so it only applies to today's service:
    a customer can still book tomorrow's lunch after today's cutoff. A window
    with no cutoff never closes early.
    """
    if window.preorder_cutoff is None:
        return False
    tz = _preorder_timezone()
    now = now or timezone.now()
    local_now = now.astimezone(tz)
    local_booking = scheduled_for.astimezone(tz)
    if local_booking.date() != local_now.date():
        return False
    return local_now.time() >= window.preorder_cutoff


def slot_capacity_for_meal(restaurant, window):
    """Seats per slot for one meal; a per-meal override beats the kitchen limit."""
    if window.slot_capacity is not None:
        return window.slot_capacity
    return slot_capacity_for(restaurant)


def accepts_preorders(restaurant):
    """Whether at least one meal still takes pre-orders.

    Pre-orders are opt-in: a kitchen that never turned them on keeps its
    default meal windows for the schedule, but is neither advertised nor
    bookable.
    """
    if not restaurant.preorders_enabled:
        return False
    base_capacity = slot_capacity_for(restaurant)
    if base_capacity <= 0:
        return False
    return any(
        (window.slot_capacity if window.slot_capacity is not None else base_capacity) > 0
        for window in meal_windows_for(restaurant)
    )


def _day_label(day, today):
    if day == today:
        return 'Today'
    if day == today + timedelta(days=1):
        return 'Tomorrow'
    return day.strftime('%a %d %b')


def next_preorder_hint(restaurant, now=None):
    """The next meal that still takes pre-orders, for a listing badge.

    Answers "can I pre-order here soon", not "what is the full calendar": only
    today and tomorrow are considered, and the meal must have a slot the
    customer could actually book. That means a closed kitchen advertises
    nothing, today's meal must clear the cut-off and the lead time (minimum
    lead + the kitchen's prep), and the slot must have a seat left. Returns
    ``None`` when no such meal exists.
    """
    if not getattr(restaurant, 'is_active', True):
        return None
    if not accepts_preorders(restaurant):
        return None

    now = now or timezone.now()
    tz = _preorder_timezone()
    local_now = now.astimezone(tz)
    slot_minutes = settings.PREORDER_SLOT_MINUTES
    earliest = now + timedelta(
        minutes=settings.PREORDER_MIN_LEAD_MINUTES + prep_minutes_for(restaurant),
    )
    base_capacity = slot_capacity_for(restaurant)
    windows = [
        window for window in meal_windows_for(restaurant)
        if slot_capacity_for_meal(restaurant, window) > 0
    ]
    if not windows:
        return None

    # One read covers both advertised days; the feed serialises every kitchen,
    # so this must not become a query per candidate slot.
    range_start = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    range_end = datetime.combine(
        local_now.date() + timedelta(days=2), time.min, tzinfo=tz,
    )
    used = Counter()
    booked = list(Order.objects.filter(
        restaurant=restaurant,
        pool__isnull=True,
        scheduled_for__gte=range_start,
        scheduled_for__lt=range_end,
        status__in=SLOT_COUNTED_STATUSES,
    ).values_list('scheduled_for', flat=True))
    booked += list(OrderPool.objects.filter(
        restaurant=restaurant,
        scheduled_for__gte=range_start,
        scheduled_for__lt=range_end,
        status__in=SLOT_COUNTED_POOL_STATUSES,
    ).values_list('scheduled_for', flat=True))
    for scheduled_for in booked:
        slot_start, _ = slot_window_for(scheduled_for.astimezone(tz))
        used[slot_start] += 1

    for offset in range(0, 2):
        day = local_now.date() + timedelta(days=offset)
        for window in windows:
            if offset == 0:
                if window.end <= local_now.time():
                    continue
                if (
                    window.preorder_cutoff is not None
                    and local_now.time() >= window.preorder_cutoff
                ):
                    continue
            capacity = slot_capacity_for_meal(restaurant, window)
            cursor = datetime.combine(day, window.start, tzinfo=tz)
            window_end = datetime.combine(day, window.end, tzinfo=tz)
            while cursor < window_end:
                slot_end = cursor + timedelta(minutes=slot_minutes)
                if slot_end > window_end:
                    break
                if cursor >= earliest and used.get(cursor, 0) < capacity:
                    return {
                        'accepting': True,
                        'meal': window.label,
                        'cutoff': (
                            window.preorder_cutoff.strftime('%H:%M')
                            if window.preorder_cutoff else None
                        ),
                        'day': _day_label(day, local_now.date()),
                    }
                cursor = slot_end
    return None


def upcoming_slot_days(restaurant, days=2, now=None):
    """Bookable eating-time slots for the next ``days`` days.

    Slots are grouped by day and, inside a day, by meal (breakfast, lunch,
    dinner). ``days[].slots`` stays flat for older clients; every slot also
    carries its meal, so either shape can be rendered. Days and labels are in
    the restaurant's timezone. Returns ``[]`` when pre-orders are disabled.
    """
    now = now or timezone.now()
    slot_minutes = settings.PREORDER_SLOT_MINUTES
    if not accepts_preorders(restaurant):
        return []

    windows = meal_windows_for(restaurant)
    base_capacity = slot_capacity_for(restaurant)
    tz = _preorder_timezone()
    local_now = now.astimezone(tz)
    # A bookable slot must leave enough time for the kitchen's slowest section
    # to prep, not just the customer's minimum lead window.
    earliest = now + timedelta(
        minutes=settings.PREORDER_MIN_LEAD_MINUTES + prep_minutes_for(restaurant),
    )
    horizon = now + timedelta(days=settings.PREORDER_MAX_HORIZON_DAYS)

    first_day = local_now.date()
    last_day = horizon.astimezone(tz).date()
    day_count = max(1, min(int(days), (last_day - first_day).days + 1))

    used = Counter()
    booked = list(Order.objects.filter(
        restaurant=restaurant,
        pool__isnull=True,
        scheduled_for__gt=now,
        scheduled_for__lte=horizon + timedelta(minutes=slot_minutes),
        status__in=SLOT_COUNTED_STATUSES,
    ).values_list('scheduled_for', flat=True))
    booked += list(OrderPool.objects.filter(
        restaurant=restaurant,
        scheduled_for__gt=now,
        scheduled_for__lte=horizon + timedelta(minutes=slot_minutes),
        status__in=SLOT_COUNTED_POOL_STATUSES,
    ).values_list('scheduled_for', flat=True))
    for scheduled_for in booked:
        slot_start, _ = slot_window_for(scheduled_for.astimezone(tz))
        used[slot_start] += 1

    result = []
    for offset in range(day_count):
        day = first_day + timedelta(days=offset)
        slots = []
        meals = []
        for window in windows:
            capacity = (
                window.slot_capacity
                if window.slot_capacity is not None
                else base_capacity
            )
            if capacity <= 0:
                continue

            # Today's meal stops taking pre-orders at its configured cutoff
            # (e.g. lunch at 11:00). Future days are unaffected.
            day_cutoff_passed = (
                window.preorder_cutoff is not None
                and day == local_now.date()
                and local_now.time() >= window.preorder_cutoff
            )
            if day_cutoff_passed:
                continue

            window_slots = []
            cursor = datetime.combine(day, window.start, tzinfo=tz)
            window_end = datetime.combine(day, window.end, tzinfo=tz)
            while cursor < window_end:
                slot_end = cursor + timedelta(minutes=slot_minutes)
                if slot_end > window_end:
                    break
                if cursor >= earliest and cursor <= horizon:
                    used_count = used.get(cursor, 0)
                    window_slots.append({
                        'start': cursor.isoformat(),
                        'end': slot_end.isoformat(),
                        'label': cursor.strftime('%H:%M'),
                        'meal_period': window.kind,
                        'meal_label': window.label,
                        'capacity': capacity,
                        'remaining': max(capacity - used_count, 0),
                        'available': used_count < capacity,
                    })
                cursor = slot_end

            if window_slots:
                slots.extend(window_slots)
                meals.append({
                    'type': window.kind,
                    'label': window.label,
                    'start_time': window.start.strftime('%H:%M'),
                    'end_time': window.end.strftime('%H:%M'),
                    'slots': window_slots,
                })

        if slots:
            result.append({
                'date': day.isoformat(),
                'label': _day_label(day, local_now.date()),
                'slots': slots,
                'meals': meals,
            })
    return result


def slot_seats_taken(restaurant, slot_start, slot_end):
    """Seats booked in a slot. A group bucket counts as a single seat."""
    orders = Order.objects.filter(
        restaurant=restaurant,
        pool__isnull=True,
        scheduled_for__gte=slot_start,
        scheduled_for__lt=slot_end,
        status__in=SLOT_COUNTED_STATUSES,
    ).count()
    pools = OrderPool.objects.filter(
        restaurant=restaurant,
        scheduled_for__gte=slot_start,
        scheduled_for__lt=slot_end,
        status__in=SLOT_COUNTED_POOL_STATUSES,
    ).count()
    return orders + pools


def ensure_preorder_slot_capacity(restaurant, scheduled_for, now=None):
    """
    Refuse a booking outside a serving window, after its pre-order cutoff, or
    when its slot is full. The caller must hold the restaurant row lock so
    concurrent bookings are serialised.
    """
    if not restaurant.preorders_enabled:
        raise PreorderSlotUnavailable(
            "This restaurant is not accepting pre-orders."
        )

    base_capacity = slot_capacity_for(restaurant)
    if base_capacity <= 0:
        raise PreorderSlotUnavailable(
            "This restaurant is not accepting pre-orders."
        )

    window = meal_window_for(restaurant, scheduled_for)
    if window is None:
        windows = meal_windows_for(restaurant)
        if not windows:
            raise PreorderSlotUnavailable(
                "This restaurant is not accepting pre-orders."
            )
        raise PreorderSlotUnavailable(
            "Pre-orders are only accepted during "
            + ", ".join(candidate.label.lower() for candidate in windows)
            + "."
        )

    capacity = slot_capacity_for_meal(restaurant, window)
    if capacity <= 0:
        raise PreorderSlotUnavailable(
            f"{window.label} pre-orders are not being accepted."
        )

    if meal_cutoff_passed(window, scheduled_for, now=now):
        raise PreorderSlotUnavailable(
            f"{window.label} pre-orders close at "
            f"{window.preorder_cutoff.strftime('%H:%M')}."
        )

    slot_start, slot_end = slot_window_for(scheduled_for)
    used = slot_seats_taken(restaurant, slot_start, slot_end)
    if used >= capacity:
        raise PreorderSlotUnavailable(
            "This time slot is fully booked. Please choose another time."
        )
    return capacity - used - 1


def activate_paid_order(order):
    """
    Hand a paid order to the live kitchen pipeline. Shared by payment
    collection (immediate orders) and the release sweep (pre-orders).

    The caller must hold the row lock and the order must already be saved
    with is_paid=True.
    """
    from domain.services.orders.order_placement import ConfirmOrderService

    if order.status not in ('CREATED', 'AWAITING_CONFIRMATION'):
        order.status = OrderFSM.transition(order.status, 'CREATED')

    if (
        order.restaurant
        and (
            order.restaurant.source_type == 'PLATFORM_MANAGED'
            or order.restaurant.settlement_mode == 'PLATFORM_PREPAID'
        )
        and order.confirmation_status != Order.CONFIRMATION_CONFIRMED
    ):
        if order.status == 'CREATED':
            order.status = OrderFSM.transition(order.status, 'AWAITING_CONFIRMATION')
        order.confirmation_status = Order.CONFIRMATION_PENDING
        # The ops-confirmation timeout measures from this timestamp; a
        # pre-order created days ago must still get its full window.
        order.status_changed_at = timezone.now()
        order.save(update_fields=[
            'status', 'status_changed_at', 'confirmation_status', 'is_paid',
        ])
        try:
            from domain.services.order_sms_notifications import OrderPrepSmsService
            OrderPrepSmsService.notify_single_order(str(order.id))
        except Exception as se:
            logger.warning(f"Could not dispatch SMS for concierge order {order.id}: {se}")
        return order

    order.save(update_fields=['status', 'is_paid'])
    return ConfirmOrderService.execute(order.id, order.customer)


class ReleaseScheduledOrderService:
    @staticmethod
    def execute(order_id):
        with transaction.atomic():
            order = (
                Order.objects.select_for_update()
                .select_related('restaurant', 'customer')
                .get(id=order_id)
            )
            if order.status != 'SCHEDULED':
                return order
            if not order.is_paid:
                logger.warning(f"[PreOrder] Skipping unpaid scheduled order {order.id}")
                return order

            order.status = OrderFSM.transition(order.status, 'CREATED')
            # The CONFIRMED→PREPARING grace sweep measures from this timestamp;
            # without the reset a pre-order booked days ago would be treated as
            # already overdue the moment it is released.
            order.status_changed_at = timezone.now()
            order.save(update_fields=['status', 'status_changed_at', 'is_paid'])
            return activate_paid_order(order)


# Declining is only safe before the kitchen starts cooking. PREPARING (and
# anything later) is excluded so a vendor cannot cancel food that is already
# being made.
DECLINABLE_STATUSES = (
    'SCHEDULED',
    'AWAITING_CONFIRMATION',
    'CONFIRMED',
)


class PreorderDeclineService:
    """Kitchen declines a pre-order before it is cooked — always a full refund.

    Restricted to pre-orders: a live order in the same state still goes through
    the normal ``reject`` flow, where fees and reliability strikes apply.
    """

    @staticmethod
    def execute(order_id, actor, reason='Pre-order declined by restaurant'):
        from domain.services.orders.order_cancellation import (
            CancelOrderService,
            _is_vendor_or_staff,
        )

        with transaction.atomic():
            order = (
                Order.objects.select_for_update()
                .select_related('restaurant')
                .get(id=order_id)
            )
            if not order.scheduled_for:
                raise ValueError("This order is not a pre-order.")
            if order.status not in DECLINABLE_STATUSES:
                raise ValueError(
                    f"Cannot decline a pre-order in {order.status} state."
                )
            is_admin = (
                getattr(actor, 'role', None) == 'admin'
                or getattr(actor, 'is_staff', False)
            )
            if not (is_admin or _is_vendor_or_staff(actor, order)):
                raise PermissionError(
                    "Only restaurant staff can decline this pre-order."
                )

        # A vendor cancellation never charges a cancel fee, so the customer is
        # refunded the full amount. CancelOrderService re-locks and is a no-op
        # if a concurrent decline already cancelled the order.
        return CancelOrderService.execute(
            order_id,
            actor,
            reason=reason,
            category='vendor_rejected',
            fault='VENDOR',
        )
