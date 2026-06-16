from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display
from leaflet.admin import LeafletGeoAdminMixin
from .models import Driver, DeliveryAssignment, DeliveryProof


class DeliveryAssignmentInline(TabularInline):
    model = DeliveryAssignment
    extra = 0
    fields = ('order', 'assigned_at', 'picked_at', 'delivered_at')
    readonly_fields = ('assigned_at',)
    raw_id_fields = ('order',)
    show_change_link = True


@admin.register(Driver)
class DriverAdmin(LeafletGeoAdminMixin, ModelAdmin):
    settings_overrides = {
        'DEFAULT_CENTER': (-6.7924, 39.2083),
        'DEFAULT_ZOOM': 13,
    }
    list_display = ('display_header', 'show_status', 'vehicle_type', 'rating', 'total_earnings', 'assignment_count')
    list_filter = ('status', 'vehicle_type')
    search_fields = ('user__username', 'user__email', 'user__phone', 'vehicle_number')
    raw_id_fields = ('user',)
    readonly_fields = ('id', 'total_earnings', 'today_earnings')
    list_per_page = 25
    inlines = [DeliveryAssignmentInline]

    fieldsets = (
        (None, {'fields': ('id', 'user', 'status', 'home_restaurant', 'serves_all', 'can_assign')}),
        ('Vehicle Info', {'fields': ('vehicle_type', 'vehicle_number')}),
        ('Capacity & Quality', {'fields': ('max_active_orders', 'current_active_orders', 'rating')}),
        ('Financials & Stats', {'fields': ('total_earnings', 'today_earnings')}),
        ('Location', {'fields': ('location',)}),
    )

    actions = ['set_online', 'set_offline']

    @display(header=True, description="Driver")
    def display_header(self, obj):
        return (
            obj.user.username,
            obj.user.email or obj.user.phone or '',
            obj.user.username[:2].upper(),
        )

    @display(
        description="Status",
        ordering="status",
        label={
            "ONLINE": "success",
            "OFFLINE": "warning",
            "AVAILABLE": "info",
            "DELIVERING": "primary",
        },
    )
    def show_status(self, obj):
        return obj.status

    @display(description="Deliveries")
    def assignment_count(self, obj):
        return obj.assignments.count()

    @admin.action(description='🟢 Set selected drivers ONLINE')
    def set_online(self, request, queryset):
        updated = queryset.update(status='ONLINE')
        self.message_user(request, f'{updated} driver(s) set online.')

    @admin.action(description='⚫ Set selected drivers OFFLINE')
    def set_offline(self, request, queryset):
        updated = queryset.exclude(status='DELIVERING').update(status='OFFLINE')
        self.message_user(request, f'{updated} driver(s) set offline.')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user').prefetch_related('assignments')


@admin.register(DeliveryAssignment)
class DeliveryAssignmentAdmin(ModelAdmin):
    list_display = ('short_id', 'order', 'driver', 'assigned_at', 'picked_at', 'delivered_at', 'show_progress')
    list_filter = ('assigned_at',)
    search_fields = ('order__id', 'driver__user__username')
    raw_id_fields = ('order', 'driver')
    readonly_fields = ('id', 'assigned_at')
    date_hierarchy = 'assigned_at'
    list_per_page = 25

    @display(description="ID")
    def short_id(self, obj):
        return str(obj.id)[:8] + '…'

    @display(
        description="Progress",
        label={
            "Delivered": "success",
            "In Transit": "warning",
            "Assigned": "info",
        },
    )
    def show_progress(self, obj):
        if obj.delivered_at:
            return "Delivered"
        if obj.picked_at:
            return "In Transit"
        return "Assigned"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('order', 'driver', 'driver__user')


@admin.register(DeliveryProof)
class DeliveryProofAdmin(ModelAdmin):
    list_display = ('short_id', 'order', 'delivered_by', 'photo_link', 'verified_at', 'created_at')
    list_filter = ('verified_at', 'created_at')
    search_fields = ('order__id', 'delivered_by__user__username')
    raw_id_fields = ('order', 'delivered_by')
    readonly_fields = ('id', 'created_at')
    date_hierarchy = 'created_at'

    @display(description="ID")
    def short_id(self, obj):
        return str(obj.id)[:8] + '…'

    def photo_link(self, obj):
        if obj.photo_url:
            return format_html('<a href="{}" target="_blank">📷 View</a>', obj.photo_url)
        return '-'
    photo_link.short_description = 'Photo'

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('order', 'delivered_by', 'delivered_by__user')
