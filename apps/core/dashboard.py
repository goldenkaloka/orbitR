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
    Custom dashboard for Unfold Admin with Graphs.
    """
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = (now - timedelta(days=7)).date()
    
    # --- 1. Metrics ---
    today_revenue = Payment.objects.filter(
        status='COLLECTED',
        created_at__gte=today_start
    ).aggregate(total=Sum('amount'))['total'] or 0
    
    active_orders_count = Order.objects.filter(
        status__in=['CREATED', 'CONFIRMED', 'PREPARING', 'READY', 'PICKED']
    ).count()
    
    new_users_count = User.objects.filter(date_joined__gte=now - timedelta(days=7)).count()
    open_pools_count = OrderPool.objects.filter(status='OPEN').count()

    # --- 2. Chart Data: Orders Last 7 Days ---
    last_7_days_orders = (
        Order.objects.filter(created_at__date__gte=week_ago)
        .annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(count=Count('id'))
        .order_by('date')
    )
    
    labels = []
    order_data = []
    # Fill in potential gaps in dates
    for i in range(7, -1, -1):
        d = (now - timedelta(days=i)).date()
        labels.append(d.strftime("%b %d"))
        
        match = next((x for x in last_7_days_orders if x['date'] == d), None)
        order_data.append(match['count'] if match else 0)

    # --- 3. Chart Data: Payment Status Breakdown ---
    payment_breakdown = (
        Payment.objects.values('status')
        .annotate(count=Count('id'))
    )
    
    payment_labels = [p['status'] for p in payment_breakdown]
    payment_counts = [p['count'] for p in payment_breakdown]

    return [
        {
            "title": _("Key Performance Indicators"),
            "items": [
                {
                    "title": _("Today's Revenue"),
                    "value": f"TSh {int(today_revenue):,}",
                    "icon": "payments",
                },
                {
                    "title": _("Active Orders"),
                    "value": active_orders_count,
                    "icon": "shopping_cart",
                },
                {
                    "title": _("New Users (7d)"),
                    "value": new_users_count,
                    "icon": "person_add",
                },
                {
                    "title": _("Open Group Pools"),
                    "value": open_pools_count,
                    "icon": "group",
                },
            ],
        },
        {
            "title": _("Order Trends"),
            "items": [
                {
                    "title": _("Orders (Last 7 Days)"),
                    "chart_id": "order_trends",
                    "data": {
                        "labels": labels,
                        "datasets": [
                            {
                                "label": _("Orders"),
                                "data": order_data,
                            }
                        ]
                    }
                },
            ],
        },
        {
            "title": _("Payment Health"),
            "items": [
                {
                    "title": _("Transaction Statuses"),
                    "chart_id": "payment_status",
                    "type": "pie",
                    "data": {
                        "labels": payment_labels,
                        "datasets": [
                            {
                                "label": _("Volume"),
                                "data": payment_counts,
                            }
                        ]
                    }
                },
            ],
        },
    ]
