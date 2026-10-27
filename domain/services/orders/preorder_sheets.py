"""Printable kitchen sheets for pre-orders.

A meal stops taking pre-orders at its cut-off (or at the end of its serving
window when none is configured). At that moment the kitchen needs one complete
list it can print and cook from: the totals to prepare and which order carries
what. The sheet is rendered once and stored, so every later download returns
the same list even if an order is declined afterwards.
"""

import io
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from apps.orders.models import Order, PreorderSheet
from apps.restaurants.models import Restaurant
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from domain.services.orders.scheduled_orders import meal_windows_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

# An unpaid cart never reached the kitchen, and a cancelled/refunded order is
# not cooked. Everything else that is paid belongs on the sheet.
EXCLUDED_SHEET_STATUSES = ('PENDING_PAYMENT', 'CANCELLED', 'REFUNDED', 'EXPIRED')


@dataclass
class SheetLine:
    name: str
    quantity: int
    price: Decimal


@dataclass
class SheetOrderRow:
    order: Order
    lines: list


@dataclass
class SheetData:
    rows: list
    item_count: int
    subtotal: Decimal
    dish_totals: list


def _preorder_timezone():
    return ZoneInfo(getattr(settings, 'PREORDER_TIMEZONE', 'Africa/Dar_es_Salaam'))


def meal_locked_at(service_date, window, tz=None):
    """When this meal's pre-orders become final for the given date."""
    tz = tz or _preorder_timezone()
    return datetime.combine(
        service_date, window.preorder_cutoff or window.end, tzinfo=tz,
    )


def sheet_orders(restaurant, window, service_date, tz=None):
    """Paid pre-orders booked into one meal's serving window."""
    tz = tz or _preorder_timezone()
    start = datetime.combine(service_date, window.start, tzinfo=tz)
    end = datetime.combine(service_date, window.end, tzinfo=tz)
    return (
        Order.objects.filter(
            restaurant=restaurant,
            is_paid=True,
            scheduled_for__gte=start,
            scheduled_for__lt=end,
        )
        .exclude(status__in=EXCLUDED_SHEET_STATUSES)
        .select_related('customer', 'pool')
        .prefetch_related('items__food')
        .order_by('scheduled_for', 'created_at')
    )


def collect_sheet_data(orders):
    """Per-order lines plus the kitchen-wide totals they roll up to."""
    rows = []
    dish_totals = Counter()
    item_count = 0
    subtotal = Decimal('0')

    for order in orders:
        lines = []
        for item in order.items.all():
            quantity = item.active_quantity
            if quantity <= 0:
                continue
            name = item.food.name if item.food_id else 'Removed item'
            lines.append(SheetLine(name=name, quantity=quantity, price=item.price))
            dish_totals[name] += quantity
            item_count += quantity
            subtotal += item.price * quantity
        if lines:
            rows.append(SheetOrderRow(order=order, lines=lines))

    return SheetData(
        rows=rows,
        item_count=item_count,
        subtotal=subtotal,
        dish_totals=sorted(dish_totals.items(), key=lambda entry: (-entry[1], entry[0])),
    )


def _safe(value):
    """Escape for Paragraph and drop characters the built-in fonts cannot draw."""
    text = escape(str(value or ''))
    return text.encode('cp1252', 'replace').decode('cp1252')


def _money(value):
    return f'TZS {value:,.0f}'


def _styles():
    return {
        'eyebrow': ParagraphStyle(
            'sheet-eyebrow', fontName='Helvetica-Bold', fontSize=8,
            leading=10, textColor=colors.HexColor('#B4123F'),
        ),
        'title': ParagraphStyle(
            'sheet-title', fontName='Helvetica-Bold', fontSize=21,
            leading=25, textColor=colors.HexColor('#1B1B1B'),
        ),
        'subtitle': ParagraphStyle(
            'sheet-subtitle', fontName='Helvetica', fontSize=11,
            leading=14, textColor=colors.HexColor('#4A4A4A'),
        ),
        'meta': ParagraphStyle(
            'sheet-meta', fontName='Helvetica', fontSize=8.5,
            leading=11, textColor=colors.HexColor('#6B6B6B'),
        ),
        'section': ParagraphStyle(
            'sheet-section', fontName='Helvetica-Bold', fontSize=12,
            leading=15, textColor=colors.HexColor('#1B1B1B'),
            spaceBefore=14, spaceAfter=6,
        ),
        'cell': ParagraphStyle(
            'sheet-cell', fontName='Helvetica', fontSize=8.5,
            leading=11, textColor=colors.HexColor('#1B1B1B'),
        ),
        'cell_muted': ParagraphStyle(
            'sheet-cell-muted', fontName='Helvetica', fontSize=7.5,
            leading=9.5, textColor=colors.HexColor('#7A7A7A'),
        ),
        'total_label': ParagraphStyle(
            'sheet-total-label', fontName='Helvetica', fontSize=8,
            leading=10, textColor=colors.HexColor('#6B6B6B'),
        ),
        'total_value': ParagraphStyle(
            'sheet-total-value', fontName='Helvetica-Bold', fontSize=14,
            leading=17, textColor=colors.HexColor('#1B1B1B'),
        ),
    }


def _summary_table(data, styles):
    cells = [
        [Paragraph('ORDERS', styles['total_label']),
         Paragraph(str(len(data.rows)), styles['total_value'])],
        [Paragraph('ITEMS', styles['total_label']),
         Paragraph(str(data.item_count), styles['total_value'])],
        [Paragraph('PRE-ORDER VALUE', styles['total_label']),
         Paragraph(_money(data.subtotal), styles['total_value'])],
    ]
    column = (A4[0] - 28 * mm) / 3
    summary = Table(
        [[cell[0] for cell in cells], [cell[1] for cell in cells]],
        colWidths=[column] * 3,
    )
    summary.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F7F3EE')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#E4DBD1')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E4DBD1')),
        ('TOPPADDING', (0, 0), (-1, 0), 7),
        ('BOTTOMPADDING', (0, 1), (-1, 1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 9),
    ]))
    return summary


def _dish_table(data, styles):
    rows = [[Paragraph('<b>Dish</b>', styles['cell']),
             Paragraph('<b>Qty</b>', styles['cell'])]]
    for name, quantity in data.dish_totals:
        rows.append([
            Paragraph(_safe(name), styles['cell']),
            Paragraph(str(quantity), styles['cell']),
        ])
    table = Table(rows, colWidths=[142 * mm, 40 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F3EFE8')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ('LINEBELOW', (0, 0), (-1, -1), 0.25, colors.HexColor('#E2DBD2')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    return table


def _orders_table(data, styles, tz):
    header = ['#', 'Time', 'Customer', 'Items', 'Notes']
    rows = [[Paragraph(f'<b>{label}</b>', styles['cell']) for label in header]]

    for index, row in enumerate(data.rows, start=1):
        order = row.order
        local_time = order.scheduled_for.astimezone(tz)
        fulfilment = 'Pickup' if order.fulfillment_type == 'PICKUP' else 'Delivery'

        customer = [f'<b>{_safe(order.customer.get_full_name() or order.customer.username)}</b>']
        if getattr(order.customer, 'phone', None):
            customer.append(
                f'<font color="#7A7A7A">{_safe(order.customer.phone)}</font>'
            )
        if order.pool_id:
            customer.append(
                f'<font color="#7A7A7A">Group {_safe(order.pool.invite_code)}</font>'
            )

        items = '<br/>'.join(
            f'{line.quantity}&times; {_safe(line.name)}' for line in row.lines
        )
        notes = _safe(order.notes).replace('\n', '<br/>') or '&mdash;'

        rows.append([
            Paragraph(str(index), styles['cell']),
            Paragraph(
                f'{local_time.strftime("%H:%M")}<br/>'
                f'<font color="#7A7A7A">{fulfilment}</font>',
                styles['cell'],
            ),
            Paragraph('<br/>'.join(customer), styles['cell']),
            Paragraph(items, styles['cell']),
            Paragraph(notes, styles['cell']),
        ])

    table = Table(
        rows,
        colWidths=[7 * mm, 17 * mm, 42 * mm, 76 * mm, 40 * mm],
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F3EFE8')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (1, -1), 'RIGHT'),
        ('LINEBELOW', (0, 0), (-1, -1), 0.25, colors.HexColor('#E2DBD2')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    return table


def build_sheet_pdf(restaurant, window, service_date, data, generated_at=None, tz=None):
    """Render one meal's kitchen sheet as PDF bytes."""
    tz = tz or _preorder_timezone()
    generated_at = generated_at or timezone.now()
    styles = _styles()
    locked_at = meal_locked_at(service_date, window, tz)

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title=f'{window.label} pre-orders - {restaurant.name}',
        author=restaurant.name,
    )

    story = [
        Paragraph('PRE-ORDER KITCHEN SHEET', styles['eyebrow']),
        Spacer(1, 2),
        Paragraph(_safe(restaurant.name), styles['title']),
        Paragraph(
            f'{_safe(window.label)} &middot; '
            f'{service_date.strftime("%A %d %B %Y")}',
            styles['subtitle'],
        ),
        Spacer(1, 3),
        Paragraph(
            f'Bookings closed {locked_at.strftime("%H:%M")} &middot; '
            f'Generated {generated_at.astimezone(tz).strftime("%d %b %Y %H:%M")}',
            styles['meta'],
        ),
        Spacer(1, 10),
        _summary_table(data, styles),
        Paragraph('What to cook', styles['section']),
        _dish_table(data, styles),
        Paragraph('Orders', styles['section']),
        _orders_table(data, styles, tz),
    ]

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#FF2D55'))
        canvas.setLineWidth(1.4)
        canvas.line(14 * mm, A4[1] - 9 * mm, A4[0] - 14 * mm, A4[1] - 9 * mm)
        canvas.setFont('Helvetica', 7.5)
        canvas.setFillColor(colors.HexColor('#7A7A7A'))
        canvas.drawString(
            14 * mm, 9 * mm,
            _safe(f'{restaurant.name} - {window.label} pre-orders'),
        )
        canvas.drawRightString(A4[0] - 14 * mm, 9 * mm, f'Page {doc.page}')
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def _store_sheet(restaurant, window, service_date, locked_at, data, generated_at):
    pdf = build_sheet_pdf(
        restaurant, window, service_date, data, generated_at=generated_at,
    )
    sheet = PreorderSheet(
        restaurant=restaurant,
        service_date=service_date,
        meal_period=window.kind,
        meal_label=window.label,
        locked_at=locked_at,
        order_count=len(data.rows),
        item_count=data.item_count,
        subtotal=data.subtotal,
        pdf=pdf,
    )
    try:
        with transaction.atomic():
            sheet.save()
    except IntegrityError:
        # Another worker stored the same meal first; its sheet wins.
        logger.info(
            '[PreorderSheet] Sheet already exists for %s %s %s',
            restaurant.id, service_date, window.kind,
        )
        return None
    return sheet


def notify_sheet_ready(sheet):
    """Tell the kitchen the list is final and where to get it."""
    from apps.restaurants.models import SectionStaff
    from infrastructure.notifications.service import NotificationService

    recipients = {sheet.restaurant.owner_id}
    recipients |= set(
        SectionStaff.objects.filter(section__restaurant=sheet.restaurant)
        .values_list('user_id', flat=True)
    )
    recipients.discard(None)

    title = f'{sheet.meal_label} pre-orders are locked'
    body = (
        f'{sheet.order_count} orders, {sheet.item_count} items. '
        'Your kitchen sheet is ready to download.'
    )
    data = {
        'type': 'preorder_sheet_ready',
        'sheet_id': str(sheet.id),
        'service_date': sheet.service_date.isoformat(),
        'meal_period': sheet.meal_period,
    }
    for user_id in recipients:
        try:
            NotificationService.send_push(
                user_id=str(user_id), title=title, body=body, data=data,
            )
        except Exception as exc:
            logger.warning(
                '[PreorderSheet] Could not notify %s for sheet %s: %s',
                user_id, sheet.id, exc,
            )


def generate_due_sheets(now=None, notify=True):
    """Create today's sheets for every meal whose booking window has closed."""
    now = now or timezone.now()
    tz = _preorder_timezone()
    local_now = now.astimezone(tz)
    service_date = local_now.date()
    day_start = datetime.combine(service_date, time(0, 0), tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    # Only restaurants with real bookings for today can produce a sheet; this
    # keeps the beat task from touching the whole restaurant table every run.
    restaurant_ids = list(
        Order.objects.filter(
            is_paid=True,
            scheduled_for__gte=day_start,
            scheduled_for__lt=day_end,
        )
        .exclude(status__in=EXCLUDED_SHEET_STATUSES)
        .values_list('restaurant_id', flat=True)
        .distinct()
    )
    if not restaurant_ids:
        return []

    created = []
    restaurants = (
        Restaurant.objects.filter(id__in=restaurant_ids)
        .prefetch_related('meal_periods', 'sections')
    )
    for restaurant in restaurants:
        for window in meal_windows_for(restaurant):
            locked_at = meal_locked_at(service_date, window, tz)
            if local_now < locked_at:
                continue
            if PreorderSheet.objects.filter(
                restaurant=restaurant,
                service_date=service_date,
                meal_period=window.kind,
            ).exists():
                continue

            orders = list(sheet_orders(restaurant, window, service_date, tz))
            data = collect_sheet_data(orders)
            if not data.rows:
                continue

            sheet = _store_sheet(
                restaurant, window, service_date, locked_at, data, now,
            )
            if sheet is None:
                continue
            created.append(sheet)
            if notify:
                notify_sheet_ready(sheet)

    if created:
        logger.info('[PreorderSheet] Generated %s sheet(s) for %s', len(created), service_date)
    return created
