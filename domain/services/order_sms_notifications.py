import logging
from collections import defaultdict
from decimal import Decimal

from infrastructure.notifications.service import NotificationService

logger = logging.getLogger(__name__)

MAX_SMS_LENGTH = 1500


def _clean_phone(phone):
    if not phone:
        return None
    return str(phone).strip() or None


def _short_id(value):
    return str(value).split('-')[0].upper()


def _money(value):
    try:
        amount = Decimal(str(value or 0))
    except Exception:
        amount = Decimal('0')
    return f"{int(amount):,}".replace(',', ',')


def _truncate(message):
    if len(message) <= MAX_SMS_LENGTH:
        return message
    return message[:MAX_SMS_LENGTH - 30].rstrip() + "\n...Fungua app kuona yote."


def _food_name(item):
    food = getattr(item, 'food', None)
    return food.name if food else 'Bidhaa'


def _time_str(dt):
    if not dt:
        return ''
    return dt.strftime('%d/%m %H:%M')


def _send_once(messages_by_phone):
    sent = set()
    for phone, message in messages_by_phone.items():
        clean_phone = _clean_phone(phone)
        if not clean_phone or clean_phone in sent:
            continue
        sent.add(clean_phone)
        try:
            NotificationService.send_sms(clean_phone, _truncate(message))
        except Exception as exc:
            logger.error("Failed to send order prep SMS", extra={'phone': clean_phone, 'error': str(exc)})


class OrderPrepSmsService:
    @staticmethod
    def notify_single_order(order_id):
        from apps.orders.models import Order
        from apps.restaurants.models import SectionStaff

        order = (
            Order.objects.select_related('restaurant__owner', 'customer')
            .prefetch_related('items__food', 'items__section')
            .get(id=order_id)
        )
        items = list(order.items.all())
        short = _short_id(order.id)
        customer_name = order.customer.username or order.customer.phone or 'Mteja'
        delivery_note = (
            'Kuchukuliwa na mteja'
            if order.fulfillment_type == 'PICKUP'
            else (order.delivery_address or 'Kufikishwa kwa mteja')
        )
        item_lines = [
            f"- {item.quantity}x {_food_name(item)}"
            for item in items
        ]
        customer_phone = _clean_phone(order.customer.phone)
        order_time = _time_str(order.confirmed_at or order.created_at)
        prep_time = order.restaurant.average_prep_time or 20

        owner_message = "\n".join([
            f"OrbitR: Oda mpya imelipwa #{short}",
            f"Mteja: {customer_name}",
            f"Namba: {customer_phone or '---'}",
            f"Jumla ya chakula: TSh {_money(order.items_subtotal)}",
            f"Wakati: {order_time}",
            f"Makadirio: {prep_time}dak",
            f"Aina: {delivery_note}",
            "Bidhaa:",
            *(item_lines or ["- Hakuna bidhaa"]),
            "Tafadhali anza kuandaa sasa.",
        ])

        messages = {}
        owner_phone = _clean_phone(order.restaurant.owner.phone)
        if owner_phone:
            messages[owner_phone] = owner_message

        section_ids = {item.section_id for item in items if item.section_id}
        staff_rows = (
            SectionStaff.objects.filter(section_id__in=section_ids)
            .select_related('user', 'section')
        )
        staff_lines = defaultdict(list)
        for item in items:
            if not item.section_id:
                continue
            for staff in staff_rows:
                if staff.section_id == item.section_id:
                    staff_lines[staff].append(f"- {item.quantity}x {_food_name(item)}")

        for staff, lines in staff_lines.items():
            phone = _clean_phone(staff.user.phone)
            if not phone or phone == owner_phone:
                continue
            messages[phone] = "\n".join([
                f"OrbitR: Oda #{short} kwa eneo la {staff.section.name}",
                f"Namba mteja: {customer_phone or '---'}",
                f"Wakati: {order_time}",
                *lines,
                "Tafadhali anza kuandaa sasa.",
            ])

        _send_once(messages)

    @staticmethod
    def notify_pool_orders(pool_id):
        from apps.orders.models import Order
        from apps.restaurants.models import SectionStaff

        orders = list(
            Order.objects.filter(pool_id=pool_id)
            .select_related('restaurant__owner', 'customer', 'pool')
            .prefetch_related('items__food', 'items__section')
            .order_by('created_at')
        )
        if not orders:
            return

        first_order = orders[0]
        pool = first_order.pool
        restaurant = first_order.restaurant
        pool_short = _short_id(pool.id if pool else pool_id)
        invite = getattr(pool, 'invite_code', '') or pool_short

        first_order_time = _time_str(first_order.confirmed_at or first_order.created_at)
        prep_time = restaurant.average_prep_time or 20
        food_total = sum((order.items_subtotal or Decimal('0')) for order in orders)

        owner_lines = [
            f"OrbitR: Oda ya pamoja imelipwa #{invite}",
            f"Wateja: {len(orders)}",
            f"Jumla ya chakula: TSh {_money(food_total)}",
            f"Wakati: {first_order_time}",
            f"Makadirio: {prep_time}dak",
            "Bidhaa:",
        ]
        section_item_lines = defaultdict(list)

        for order in orders:
            customer_phone = _clean_phone(order.customer.phone)
            customer_name = order.customer.username or order.customer.phone or 'Mteja'
            for item in order.items.all():
                line = f"- {customer_name} ({customer_phone or '---'}): {item.quantity}x {_food_name(item)}"
                owner_lines.append(line)
                if item.section_id:
                    section_item_lines[item.section_id].append(line)

        owner_lines.append("Tafadhali kila stall ianze kuandaa sasa.")

        messages = {}
        owner_phone = _clean_phone(restaurant.owner.phone)
        if owner_phone:
            messages[owner_phone] = "\n".join(owner_lines)

        staff_rows = (
            SectionStaff.objects.filter(section_id__in=section_item_lines.keys())
            .select_related('user', 'section')
        )
        for staff in staff_rows:
            phone = _clean_phone(staff.user.phone)
            if not phone or phone == owner_phone:
                continue
            lines = section_item_lines.get(staff.section_id, [])
            if not lines:
                continue
            messages[phone] = "\n".join([
                f"OrbitR: Oda ya pamoja #{invite} - {staff.section.name}",
                f"Wakati: {first_order_time}",
                *lines,
                "Tafadhali anza kuandaa sasa.",
            ])

        _send_once(messages)
