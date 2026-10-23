from apps.pricing.models import ZonePricingTier
from django.contrib import admin
from leaflet.admin import LeafletGeoAdminMixin
from unfold.admin import ModelAdmin, TabularInline

from .models import OperationalZone, PlaceAlias, ZoneH3Index


class ZonePricingTierInline(TabularInline):
    model = ZonePricingTier
    extra = 1
    max_num = 1

class ZoneH3IndexInline(TabularInline):
    model = ZoneH3Index
    extra = 0
    readonly_fields = ('h3_index', 'resolution')
    can_delete = False

@admin.register(OperationalZone)
class OperationalZoneAdmin(LeafletGeoAdminMixin, ModelAdmin):
    list_display = ('name', 'is_active', 'h3_cell_count', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'description')
    inlines = [ZonePricingTierInline, ZoneH3IndexInline]

    settings_overrides = {
        'DEFAULT_CENTER': (-6.7924, 39.2026),
        'DEFAULT_ZOOM': 14,
        'DEFAULT_ZOOM_ANIMATE': True,
    }

    def h3_cell_count(self, obj):
        return obj.metadata.get('h3_count', 0)
    h3_cell_count.short_description = "H3 Cells"

    class Media:
        css = {
            'all': ('admin/css/geo_custom.css',)
        }
        js = (
            'admin/js/geo_locate.js',
            'admin/js/deck_h3_editor.js',
        )


@admin.register(PlaceAlias)
class PlaceAliasAdmin(ModelAdmin):
    list_display = (
        'display_text', 'h3_index', 'zone', 'distinct_users', 'sample_count',
        'confidence', 'status', 'last_seen',
    )
    list_filter = ('status', 'zone')
    search_fields = ('display_text', 'text_normalized')
    readonly_fields = (
        'id', 'h3_index', 'text_normalized', 'display_text', 'sample_count',
        'distinct_users', 'confidence', 'first_seen', 'last_seen',
    )
    ordering = ('-distinct_users', '-last_seen')
    list_per_page = 50
    actions = ('approve_aliases', 'reject_aliases')

    @admin.action(description='Approve selected place aliases')
    def approve_aliases(self, request, queryset):
        updated = queryset.update(status='approved')
        self.message_user(request, f'Approved {updated} place alias(es).')

    @admin.action(description='Reject selected place aliases')
    def reject_aliases(self, request, queryset):
        updated = queryset.update(status='rejected')
        self.message_user(request, f'Rejected {updated} place alias(es).')
