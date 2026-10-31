"""Printable counter receipt for a pickup order.

The customer shows this PDF at the kitchen. The ``ORD-`` code on it is the
same code the kitchen ticket carries, so handing over the bag is a code
match. The receipt is rendered from the order row on demand: nothing is
stored and no migration is involved.
"""

import io
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from django.conf import settings
from reportlab.lib import colors
from reportlab.lib.pagesizes import A5
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PAGE_MARGIN = 14 * mm
CONTENT_WIDTH = A5[0] - 2 * PAGE_MARGIN


def receipt_timezone():
    return ZoneInfo(getattr(settings, 'PREORDER_TIMEZONE', 'Africa/Dar_es_Salaam'))


def receipt_code(order):
    return f'ORD-{str(order.id)[:6].upper()}'


def receipt_filename(order):
    return f'pickup-receipt-{receipt_code(order)}.pdf'


def pickup_payment_required(order):
    """Mirror ``OrderSerializer.get_pickup_payment_required``.

    Admin-owned kitchens (company-owned / platform-prepaid) are settled
    internally, so the counter must not ask the customer for cash.
    """
    restaurant = order.restaurant if order.restaurant_id else None
    if restaurant is None:
        return True
    return restaurant.settlement_mode in (
        restaurant.SETTLEMENT_DRIVER_FLOAT,
        restaurant.SETTLEMENT_VENDOR_PAYOUT,
    )


def payment_label(order):
    if order.is_paid:
        return 'PAID', True
    if not pickup_payment_required(order):
        return 'NO PAYMENT DUE AT THE COUNTER', True
    return 'PAY AT THE COUNTER', False


def _safe(value):
    text = escape(str(value or ''))
    return text.encode('cp1252', 'replace').decode('cp1252')


def _money(value):
    return f'TZS {value:,.0f}'


def _styles():
    return {
        'eyebrow': ParagraphStyle(
            'receipt-eyebrow', fontName='Helvetica-Bold', fontSize=8,
            leading=10, textColor=colors.HexColor('#B4123F'),
        ),
        'title': ParagraphStyle(
            'receipt-title', fontName='Helvetica-Bold', fontSize=17,
            leading=20, textColor=colors.HexColor('#1B1B1B'),
        ),
        'subtitle': ParagraphStyle(
            'receipt-subtitle', fontName='Helvetica', fontSize=9,
            leading=12, textColor=colors.HexColor('#4A4A4A'),
        ),
        'meta': ParagraphStyle(
            'receipt-meta', fontName='Helvetica', fontSize=8.5,
            leading=12, textColor=colors.HexColor('#4A4A4A'),
        ),
        'code_label': ParagraphStyle(
            'receipt-code-label', fontName='Helvetica-Bold', fontSize=7.5,
            leading=9, textColor=colors.HexColor('#7A7A7A'),
        ),
        'code': ParagraphStyle(
            'receipt-code', fontName='Helvetica-Bold', fontSize=24,
            leading=28, textColor=colors.HexColor('#1B1B1B'),
        ),
        'section': ParagraphStyle(
            'receipt-section', fontName='Helvetica-Bold', fontSize=9,
            leading=12, textColor=colors.HexColor('#7A7A7A'),
            spaceBefore=12, spaceAfter=5,
        ),
        'cell': ParagraphStyle(
            'receipt-cell', fontName='Helvetica', fontSize=8.5,
            leading=11, textColor=colors.HexColor('#1B1B1B'),
        ),
        'cell_right': ParagraphStyle(
            'receipt-cell-right', fontName='Helvetica', fontSize=8.5,
            leading=11, textColor=colors.HexColor('#1B1B1B'), alignment=2,
        ),
        'muted': ParagraphStyle(
            'receipt-muted', fontName='Helvetica', fontSize=7.5,
            leading=10, textColor=colors.HexColor('#7A7A7A'),
        ),
        'total_label': ParagraphStyle(
            'receipt-total-label', fontName='Helvetica', fontSize=9,
            leading=12, textColor=colors.HexColor('#4A4A4A'),
        ),
        'total_value': ParagraphStyle(
            'receipt-total-value', fontName='Helvetica-Bold', fontSize=12,
            leading=15, textColor=colors.HexColor('#1B1B1B'), alignment=2,
        ),
    }


def _code_block(order, styles):
    table = Table(
        [[
            Paragraph('ORDER CODE', styles['code_label']),
            Paragraph(receipt_code(order), styles['code']),
        ]],
        colWidths=[30 * mm, CONTENT_WIDTH - 30 * mm],
    )
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#F7F3EE')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#E4DBD1')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
    ]))
    return table


def _meta_table(order, styles, tz):
    customer = order.customer
    customer_name = customer.get_full_name() or customer.username
    rows = [
        ('Placed', order.created_at.astimezone(tz).strftime('%d %b %Y, %H:%M')),
        ('Customer', customer_name),
    ]
    if getattr(customer, 'phone', None):
        rows.append(('Phone', customer.phone))
    rows.append(('Status', order.get_status_display()))

    table = Table(
        [
            [Paragraph(f'<b>{_safe(label)}</b>', styles['meta']),
             Paragraph(_safe(value), styles['meta'])]
            for label, value in rows
        ],
        colWidths=[26 * mm, CONTENT_WIDTH - 26 * mm],
    )
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
    ]))
    return table


def _items_table(order, styles):
    rows = [[
        Paragraph('<b>Item</b>', styles['cell']),
        Paragraph('<b>Qty</b>', styles['cell_right']),
        Paragraph('<b>Total</b>', styles['cell_right']),
    ]]
    for item in order.items.all():
        quantity = item.active_quantity
        if quantity <= 0:
            continue
        name = item.food.name if item.food_id else 'Removed item'
        rows.append([
            Paragraph(
                f'{_safe(name)}<br/>'
                f'<font color="#7A7A7A">{_money(item.price)} each</font>',
                styles['cell'],
            ),
            Paragraph(str(quantity), styles['cell_right']),
            Paragraph(_money(item.price * quantity), styles['cell_right']),
        ])

    table = Table(
        rows,
        colWidths=[CONTENT_WIDTH - 40 * mm, 16 * mm, 24 * mm],
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LINEBELOW', (0, 0), (-1, -1), 0.25, colors.HexColor('#E2DBD2')),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    return table


def _totals_table(order, styles):
    rows = [('Items', order.items_subtotal)]
    if order.service_fee:
        rows.append(('Service fee', order.service_fee))
    if order.delivery_fee:
        rows.append(('Delivery fee', order.delivery_fee))

    table_rows = [
        [Paragraph(_safe(label), styles['total_label']),
         Paragraph(_money(amount), styles['total_value'])]
        for label, amount in rows
    ]
    table_rows.append([
        Paragraph('<b>Total</b>', styles['total_label']),
        Paragraph(_money(order.total_amount), styles['total_value']),
    ])

    table = Table(table_rows, colWidths=[CONTENT_WIDTH - 40 * mm, 40 * mm])
    table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LINEABOVE', (0, len(table_rows) - 1), (-1, -1), 0.5,
         colors.HexColor('#E2DBD2')),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    return table


def _payment_badge(order, styles):
    label, settled = payment_label(order)
    color = '#1B4332' if settled else '#A9761A'
    background = '#E8F3EC' if settled else '#FBF1DE'
    table = Table(
        [[Paragraph(f'<b>{label}</b>', styles['meta'])]],
        colWidths=[CONTENT_WIDTH],
    )
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor(background)),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor(background)),
        ('TEXTCOLOR', (0, 0), (-1, -1), colors.HexColor(color)),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
    ]))
    return table


def build_pickup_receipt_pdf(order):
    """Render one pickup order's counter receipt as PDF bytes."""
    tz = receipt_timezone()
    styles = _styles()
    restaurant = order.restaurant
    restaurant_line = restaurant.location if restaurant else ''
    phone = getattr(restaurant.owner, 'phone', None) if restaurant else None

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A5,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=16 * mm,
        title=f'{receipt_code(order)} pickup receipt',
        author=restaurant.name if restaurant else 'OrbitR',
    )

    story = [
        Paragraph('PICKUP RECEIPT', styles['eyebrow']),
        Spacer(1, 2),
        Paragraph(_safe(restaurant.name if restaurant else 'OrbitR'), styles['title']),
        Paragraph(_safe(restaurant_line), styles['subtitle']),
    ]
    if phone:
        story.append(Paragraph(_safe(phone), styles['subtitle']))
    story.extend([
        Spacer(1, 10),
        _code_block(order, styles),
        Spacer(1, 5),
        Paragraph(
            'Show this code at the counter. The kitchen matches it to your '
            'ticket before handing over the bag.',
            styles['muted'],
        ),
        Spacer(1, 10),
        _meta_table(order, styles, tz),
        Paragraph('Items', styles['section']),
        _items_table(order, styles),
        Spacer(1, 4),
        _totals_table(order, styles),
        Spacer(1, 10),
        _payment_badge(order, styles),
    ])
    if order.notes and order.notes.strip():
        story.extend([
            Paragraph('Note', styles['section']),
            Paragraph(_safe(order.notes).replace('\n', '<br/>'), styles['meta']),
        ])
    story.extend([
        Spacer(1, 14),
        Paragraph(
            f'Keep this receipt until you collect your order. {receipt_code(order)}',
            styles['muted'],
        ),
    ])

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor('#FF2D55'))
        canvas.setLineWidth(1.2)
        canvas.line(PAGE_MARGIN, A5[1] - 9 * mm, A5[0] - PAGE_MARGIN, A5[1] - 9 * mm)
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(colors.HexColor('#7A7A7A'))
        canvas.drawString(
            PAGE_MARGIN, 9 * mm,
            _safe(f'{receipt_code(order)} - pickup at '
                  f'{restaurant.name if restaurant else "OrbitR"}'),
        )
        canvas.drawRightString(A5[0] - PAGE_MARGIN, 9 * mm, f'Page {doc.page}')
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()
