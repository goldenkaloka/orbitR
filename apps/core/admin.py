from django.contrib import admin
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.decorators import display
import json
from .models import IdempotencyKey


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(ModelAdmin):
    list_display = ('key_short', 'user', 'show_response_code', 'created_at')
    list_filter = ('response_code', 'created_at')
    search_fields = ('key', 'user__username', 'user__email')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 25
    raw_id_fields = ('user',)
    readonly_fields = ('id', 'key', 'user', 'response_code', 'formatted_response', 'created_at')

    fieldsets = (
        ('Key Info', {'fields': ('id', 'key', 'user')}),
        ('Response', {'fields': ('response_code', 'formatted_response')}),
        ('Timestamps', {'fields': ('created_at',)}),
    )

    @display(description="Idempotency Key")
    def key_short(self, obj):
        if len(obj.key) > 24:
            return obj.key[:24] + '…'
        return obj.key

    @display(
        description="Code",
        ordering="response_code",
        label={
            "2xx": "success",
            "4xx": "warning",
            "5xx": "danger",
        },
    )
    def show_response_code(self, obj):
        if 200 <= obj.response_code < 300:
            return "2xx", str(obj.response_code)
        elif 400 <= obj.response_code < 500:
            return "4xx", str(obj.response_code)
        return "5xx", str(obj.response_code)

    def formatted_response(self, obj):
        if isinstance(obj.response_body, dict):
            pretty = json.dumps(obj.response_body, indent=2)
        else:
            pretty = str(obj.response_body)
        return format_html('<pre style="max-height:400px; overflow:auto;">{}</pre>', pretty)
    formatted_response.short_description = 'Response Body'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
