from django.utils.translation import gettext_lazy as _
from apps.orders.models import Order, OrderPool
from apps.users.models import User
from apps.payments.models import Payment
from apps.delivery.models import Driver
from django.db.models import Sum, Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from datetime import timedelta


def dashboard_callback(request, context):
    """
    Custom Unfold dashboard — populates context with KPIs, charts, and recent data tables.
    """
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = (now - timedelta(days=7)).date()

    # ── KPI Cards ───────────────────────────────────────────────────────────
    today_revenue = Payment.objects.filter(
        status='COLLECTED',
        created_at__gte=today_start
    ).aggregate(total=Sum('amount'))['total'] or 0

    active_orders = Order.objects.filter(
        status__in=['CREATED', 'CONFIRMED', 'PREPARING', 'READY', 'PICKED']
    ).count()

    new_users_7d = User.objects.filter(date_joined__gte=now - timedelta(days=7)).count()
    total_users  = User.objects.count()
    open_pools   = OrderPool.objects.filter(status='OPEN').count()
    active_drivers = Driver.objects.filter(is_available=True).count()

    # ── Chart: Orders last 8 days ────────────────────────────────────────────
    last_8_days_qs = (
        Order.objects.filter(created_at__date__gte=week_ago)
        .annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(count=Count('id'))
        .order_by('date')
    )
    order_labels, order_data = [], []
    for i in range(7, -1, -1):
        d = (now - timedelta(days=i)).date()
        order_labels.append(d.strftime("%b %d"))
        match = next((x for x in last_8_days_qs if x['date'] == d), None)
        order_data.append(match['count'] if match else 0)

    # ── Chart: Revenue last 8 days ───────────────────────────────────────────
    last_8_days_rev = (
        Payment.objects.filter(status='COLLECTED', created_at__date__gte=week_ago)
        .annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(total=Sum('amount'))
        .order_by('date')
    )
    revenue_data = []
    for i in range(7, -1, -1):
        d = (now - timedelta(days=i)).date()
        match = next((x for x in last_8_days_rev if x['date'] == d), None)
        revenue_data.append(float(match['total']) if match else 0)

    # ── Chart: Payment status breakdown (doughnut) ───────────────────────────
    payment_breakdown = list(Payment.objects.values('status').annotate(count=Count('id')))
    payment_labels = [p['status'] for p in payment_breakdown]
    payment_counts = [p['count'] for p in payment_breakdown]

    # ── Recent Orders Table ──────────────────────────────────────────────────
    recent_orders = (
        Order.objects
        .select_related('customer', 'restaurant')
        .order_by('-created_at')[:8]
    )

    # ── Recent Users Table ───────────────────────────────────────────────────
    recent_users = User.objects.order_by('-date_joined')[:8]

    context.update({
        # KPIs
        "kpi_today_revenue":  f"TSh {int(today_revenue):,}",
        "kpi_active_orders":  active_orders,
        "kpi_new_users_7d":   new_users_7d,
        "kpi_total_users":    total_users,
        "kpi_open_pools":     open_pools,
        "kpi_active_drivers": active_drivers,
        # Charts
        "chart_order_labels":    order_labels,
        "chart_order_data":      order_data,
        "chart_revenue_data":    revenue_data,
        "chart_payment_labels":  payment_labels,
        "chart_payment_data":    payment_counts,
        # Tables
        "recent_orders": recent_orders,
        "recent_users":  recent_users,
    })

    return context
