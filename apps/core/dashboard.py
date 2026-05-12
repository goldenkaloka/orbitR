from django.utils.translation import gettext_lazy as _
from apps.orders.models import Order, OrderPool
from apps.users.models import User
from apps.payments.models import Payment
from django.db.models import Sum, Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from datetime import timedelta


def dashboard_callback(request, context):
    """
    Custom dashboard for Unfold Admin.
    Adds KPI metrics and chart data to the template context.
    """
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = (now - timedelta(days=7)).date()

    # --- 1. KPI Metrics ---
    today_revenue = Payment.objects.filter(
        status='COLLECTED',
        created_at__gte=today_start
    ).aggregate(total=Sum('amount'))['total'] or 0

    active_orders_count = Order.objects.filter(
        status__in=['CREATED', 'CONFIRMED', 'PREPARING', 'READY', 'PICKED']
    ).count()

    new_users_count = User.objects.filter(
        date_joined__gte=now - timedelta(days=7)
    ).count()

    open_pools_count = OrderPool.objects.filter(status='OPEN').count()

    # --- 2. Chart: Orders Last 7 Days ---
    last_7_days_orders = (
        Order.objects.filter(created_at__date__gte=week_ago)
        .annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(count=Count('id'))
        .order_by('date')
    )

    labels = []
    order_data = []
    for i in range(7, -1, -1):
        d = (now - timedelta(days=i)).date()
        labels.append(d.strftime("%b %d"))
        match = next((x for x in last_7_days_orders if x['date'] == d), None)
        order_data.append(match['count'] if match else 0)

    # --- 3. Chart: Payment Status Breakdown ---
    payment_breakdown = list(
        Payment.objects.values('status').annotate(count=Count('id'))
    )
    payment_labels = [p['status'] for p in payment_breakdown]
    payment_counts = [p['count'] for p in payment_breakdown]

    context.update({
        "kpi_today_revenue": f"TSh {int(today_revenue):,}",
        "kpi_active_orders": active_orders_count,
        "kpi_new_users": new_users_count,
        "kpi_open_pools": open_pools_count,
        "chart_order_labels": labels,
        "chart_order_data": order_data,
        "chart_payment_labels": payment_labels,
        "chart_payment_data": payment_counts,
    })

    return context
