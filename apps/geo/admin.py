from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from leaflet.admin import LeafletGeoAdminMixin
from .models import OperationalZone, ZoneH3Index

from apps.pricing.models import ZonePricingTier

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
            'admin/js/h3_visualizer.js',
            'admin/js/deck_h3_editor.js',
        )
