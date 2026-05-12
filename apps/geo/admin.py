from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from .models import OperationalZone, ZoneH3Index
from django.contrib.gis.admin import GISModelAdmin

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
class OperationalZoneAdmin(ModelAdmin, GISModelAdmin):
    list_display = ('name', 'is_active', 'h3_cell_count', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'description')
    inlines = [ZonePricingTierInline, ZoneH3IndexInline]
    
    def h3_cell_count(self, obj):
        return obj.metadata.get('h3_count', 0)
    h3_cell_count.short_description = "H3 Cells"

    # GISModelAdmin provides a map interface for drawing polygons
    gis_widget_kwargs = {
        'attrs': {
            'default_lon': 39.2026,
            'default_lat': -6.7924,
            'default_zoom': 14,
        }
    }

    class Media:
        js = (
            'admin/js/geo_locate.js',
            'admin/js/h3_visualizer.js',
            'admin/js/deck_h3_editor.js',
        )
