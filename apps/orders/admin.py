from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display
from leaflet.admin import LeafletGeoAdminMixin
from .models import Order, OrderItem, BatchingStrategy, Dispute


class OrderItemInline(TabularInline):
    """Inline display of items within an order."""
    model = OrderItem
    extra = 0
    fields = ('food', 'quantity', 'price')
    raw_id_fields = ('food',)


@admin.register(Order)
class OrderAdmin(LeafletGeoAdminMixin, ModelAdmin):
    settings_overrides = {
        'DEFAULT_CENTER': (-6.7924, 39.2083),
        'DEFAULT_ZOOM': 13,
    }
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
        ('Delivery', {'fields': ('delivery_address', 'delivery_location', 'driver')}),
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
        from domain.services.order_services import ConfirmOrderService
        success_count = 0
        error_count = 0
        for order in queryset:
            try:
                ConfirmOrderService.execute(order.id, request.user)
                success_count += 1
            except Exception as e:
                error_count += 1
                logger.error(f"Admin confirmation failed for order {order.id}: {str(e)}")
        
        self.message_user(request, f'Successfully confirmed {success_count} order(s). {error_count} failed.')

    @admin.action(description='❌ Mark selected orders as CANCELLED')
    def mark_cancelled(self, request, queryset):
        from domain.services.order_services import CancelOrderService
        success_count = 0
        error_count = 0
        for order in queryset:
            try:
                CancelOrderService.execute(order.id, request.user, reason="Cancelled via Admin Panel")
                success_count += 1
            except Exception as e:
                error_count += 1
                logger.error(f"Admin cancellation failed for order {order.id}: {str(e)}")
        
        self.message_user(request, f'Successfully cancelled {success_count} order(s). {error_count} failed.')

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


@admin.register(Dispute)
class DisputeAdmin(ModelAdmin):
    list_display = ('short_id', 'order', 'raised_by', 'status', 'category', 'created_at', 'assigned_to')
    list_filter = ('status', 'category', 'created_at')
    search_fields = ('id', 'order__id', 'raised_by__username', 'raised_by__email')
    ordering = ('-created_at',)
    list_per_page = 25
    raw_id_fields = ('order', 'raised_by', 'assigned_to', 'resolved_by')
    readonly_fields = ('id', 'created_at', 'updated_at', 'resolved_at', 'escalated_at')

    fieldsets = (
        ('Dispute Info', {'fields': ('id', 'order', 'raised_by', 'status', 'category')}),
        ('Details', {'fields': ('reason', 'resolution_note', 'refund_amount')}),
        ('Assignment', {'fields': ('assigned_to', 'resolved_by', 'parent_dispute')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at', 'resolved_at', 'escalated_at')}),
    )

    actions = ['mark_investigating', 'mark_escalated']

    @display(description="Dispute ID")
    def short_id(self, obj):
        return str(obj.id)[:8] + '…'

    @admin.action(description='🔍 Mark selected as INVESTIGATING')
    def mark_investigating(self, request, queryset):
        updated = queryset.filter(status='PENDING').update(status='INVESTIGATING')
        self.message_user(request, f'{updated} dispute(s) moved to INVESTIGATING')

    @admin.action(description='🚨 Mark selected as ESCALATED')
    def mark_escalated(self, request, queryset):
        from django.utils import timezone
        now = timezone.now()
        updated = queryset.filter(status__in=['PENDING', 'INVESTIGATING']).update(
            status='ESCALATED', escalated_at=now,
        )
        self.message_user(request, f'{updated} dispute(s) escalated')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('order', 'raised_by', 'assigned_to', 'resolved_by')


@admin.register(BatchingStrategy)
class BatchingStrategyAdmin(ModelAdmin):
    list_display = ('name', 'description', 'is_active', 'priority', 'cluster_by', 'h3_resolution')
    list_filter = ('is_active', 'cluster_by', 'priority')
    search_fields = ('name', 'description')
    ordering = ('-priority', 'name')
    list_editable = ('is_active', 'priority')

    fieldsets = (
        ('Basic Info', {'fields': ('name', 'description', 'is_active', 'priority')}),
        ('Clustering', {'fields': ('cluster_by', 'h3_resolution', 'neighbor_ring_k')}),
        ('Constraints', {'fields': ('constraints',)}),
        ('Pricing', {'fields': ('pricing',)}),
        ('Route Optimization', {'fields': ('route_optimization',)}),
    )
