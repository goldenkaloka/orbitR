from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display
from leaflet.admin import LeafletGeoAdminMixin
from .models import Restaurant, DeliveryZone
from apps.products.models import Food


class FoodInline(TabularInline):
    model = Food
    extra = 0
    fields = ('name', 'price', 'is_available')
    show_change_link = True

class DeliveryZoneInline(TabularInline):
    model = DeliveryZone
    extra = 0
    fields = ('name', 'fee', 'priority', 'is_active')

@admin.register(DeliveryZone)
class DeliveryZoneAdmin(ModelAdmin):
    list_display = ('name', 'restaurant', 'fee', 'priority', 'is_active')
    list_filter = ('is_active', 'restaurant')
    search_fields = ('name', 'restaurant__name')

@admin.register(Restaurant)
class RestaurantAdmin(LeafletGeoAdminMixin, ModelAdmin):
    list_display = ('display_header', 'location', 'show_active', 'food_count', 'created_at')
    list_filter = ('is_active', 'category', 'created_at')
    search_fields = ('name', 'location', 'owner__username', 'owner__email')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 25
    raw_id_fields = ('owner',)
    readonly_fields = ('id', 'created_at')
    inlines = [FoodInline, DeliveryZoneInline]

    settings_overrides = {
        'DEFAULT_CENTER': (-6.7924, 39.2083),
        'DEFAULT_ZOOM': 14,
    }

    class Media:
        js = ('admin/js/geo_locate.js',)

    fieldsets = (
        (None, {'fields': ('id', 'name', 'owner', 'description', 'image')}),
        ('Location', {'fields': ('geo_location', 'location')}),
        ('Details', {'fields': ('category', 'price_range', 'rating', 'is_active')}),
        ('Timestamps', {'fields': ('created_at',)}),
    )

    @display(header=True, description="Restaurant")
    def display_header(self, obj):
        return obj.name, f"Owner: {obj.owner.username}", obj.name[:2].upper()

    @display(description="Active", label=True)
    def show_active(self, obj):
        if obj.is_active:
            return "Active", "Active"
        return "Inactive", "Inactive"

    @display(description="Foods")
    def food_count(self, obj):
        return obj.foods.count()

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('owner').prefetch_related('foods')
