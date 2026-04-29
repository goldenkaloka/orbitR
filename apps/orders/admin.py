from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display
from .models import Order, OrderItem


class OrderItemInline(TabularInline):
    """Inline display of items within an order."""
    model = OrderItem
    extra = 0
    fields = ('food', 'quantity', 'price')
    raw_id_fields = ('food',)


@admin.register(Order)
class OrderAdmin(ModelAdmin):
    list_display = (
        'short_id', 'display_customer', 'restaurant', 'show_status',
        'total_amount', 'driver', 'created_at',
    )
    list_filter = ('status', 'created_at', 'restaurant')
    search_fields = (
        'id', 'customer__username', 'customer__email',
        'restaurant__name', 'driver__user__username',
    )
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 25
    raw_id_fields = ('customer', 'restaurant', 'driver')
    readonly_fields = ('id', 'created_at', 'items_subtotal', 'delivery_fee', 'total_amount')
    inlines = [OrderItemInline]

    fieldsets = (
        ('Order Info', {'fields': ('id', 'customer', 'restaurant', 'status')}),
        ('Financials', {
            'fields': ('items_subtotal', 'delivery_fee', 'total_amount'),
        }),
        ('Delivery', {'fields': ('delivery_address', 'driver')}),
        ('Timestamps', {'fields': ('created_at',)}),
    )

    actions = ['mark_confirmed', 'mark_cancelled']

    @display(description="Order ID")
    def short_id(self, obj):
        return str(obj.id)[:8] + '…'

    @display(header=True, description="Customer")
    def display_customer(self, obj):
        return (
            obj.customer.username,
            obj.customer.email or obj.customer.phone or '',
        )

    @display(
        description="Status",
        ordering="status",
        label={
            "CREATED": "info",
            "CONFIRMED": "success",
            "PREPARING": "warning",
            "READY_FOR_PICKUP": "warning",
            "IN_DELIVERY": "info",
            "DELIVERED": "success",
            "CANCELLED": "danger",
            "PAID": "success",
        },
    )
    def show_status(self, obj):
        return obj.status

    @admin.action(description='✅ Mark selected orders as CONFIRMED')
    def mark_confirmed(self, request, queryset):
        updated = queryset.filter(status='CREATED').update(status='CONFIRMED')
        self.message_user(request, f'{updated} order(s) confirmed.')

    @admin.action(description='❌ Mark selected orders as CANCELLED')
    def mark_cancelled(self, request, queryset):
        updated = queryset.exclude(status__in=['DELIVERED', 'CANCELLED']).update(status='CANCELLED')
        self.message_user(request, f'{updated} order(s) cancelled.')

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related('customer', 'restaurant', 'driver', 'driver__user')
            .prefetch_related('items')
        )


@admin.register(OrderItem)
class OrderItemAdmin(ModelAdmin):
    list_display = ('id', 'order', 'food', 'quantity', 'price')
    list_filter = ('order__status',)
    search_fields = ('order__id', 'food__name')
    raw_id_fields = ('order', 'food')
    readonly_fields = ('id',)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('order', 'food')
