from django.contrib import admin
from unfold.admin import ModelAdmin
from .models import OperationalZone
from django.contrib.gis.admin import GISModelAdmin

@admin.register(OperationalZone)
class OperationalZoneAdmin(ModelAdmin, GISModelAdmin):
    list_display = ('name', 'is_active', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'description')
    
    # GISModelAdmin provides a map interface for drawing polygons
    gis_widget_kwargs = {
        'attrs': {
            'default_lon': 39.2026,
            'default_lat': -6.7924,
            'default_zoom': 14,
        }
    }
