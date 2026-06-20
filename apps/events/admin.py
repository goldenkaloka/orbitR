import json

from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.decorators import display

from .models import EventLog


@admin.register(EventLog)
class EventLogAdmin(ModelAdmin):
    list_display = ('short_id', 'show_aggregate_type', 'short_aggregate_id', 'show_event_type', 'payload_preview', 'created_at')
    list_filter = ('aggregate_type', 'event_type', 'created_at')
    search_fields = ('aggregate_id', 'event_type', 'aggregate_type')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 50
    readonly_fields = ('id', 'aggregate_type', 'aggregate_id', 'event_type', 'formatted_payload', 'created_at')

    fieldsets = (
        ('Event Info', {'fields': ('id', 'aggregate_type', 'aggregate_id', 'event_type')}),
        ('Payload', {'fields': ('formatted_payload',)}),
        ('Timestamps', {'fields': ('created_at',)}),
    )

    @display(description="ID")
    def short_id(self, obj):
        return str(obj.id)[:8] + '…'

    @display(description="Aggregate", label=True)
    def show_aggregate_type(self, obj):
        return obj.aggregate_type

    @display(description="Aggregate ID")
    def short_aggregate_id(self, obj):
        return str(obj.aggregate_id)[:8] + '…'

    @display(
        description="Event",
        label={
            "OrderCreated": "info",
            "OrderConfirmed": "success",
            "OrderCancelled": "danger",
            "PaymentInitiated": "info",
            "PaymentCollected": "success",
            "PaymentFailed": "danger",
            "DriverAssigned": "warning",
            "DeliveryCompleted": "success",
        },
    )
    def show_event_type(self, obj):
        return obj.event_type

    @display(description="Payload")
    def payload_preview(self, obj):
        text = json.dumps(obj.payload) if isinstance(obj.payload, dict) else str(obj.payload)
        if len(text) > 80:
            return text[:80] + '…'
        return text

    def formatted_payload(self, obj):
        pretty = json.dumps(obj.payload, indent=2) if isinstance(obj.payload, dict) else str(obj.payload)
        return format_html('<pre style="max-height:400px; overflow:auto;">{}</pre>', pretty)
    formatted_payload.short_description = 'Payload (formatted)'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
