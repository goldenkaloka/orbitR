from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display
from leaflet.admin import LeafletGeoAdminMixin
from .models import Restaurant, RestaurantSection, SectionStaff
from apps.products.models import Food


class FoodInline(TabularInline):
    model = Food
    extra = 0
    fields = ('name', 'price', 'is_available')
    show_change_link = True


class SectionInline(TabularInline):
    model = RestaurantSection
    extra = 0
    fields = ('name', 'specialization', 'is_active', 'sort_order')
    show_change_link = True


class SectionStaffInline(TabularInline):
    model = SectionStaff
    extra = 0
    fields = ('user',)
    raw_id_fields = ('user',)


@admin.register(RestaurantSection)
class RestaurantSectionAdmin(ModelAdmin):
    list_display = ('name', 'restaurant', 'specialization', 'is_active', 'sort_order')
    list_filter = ('is_active', 'restaurant')
    search_fields = ('name', 'specialization', 'restaurant__name')
    inlines = [SectionStaffInline, FoodInline]


@admin.register(SectionStaff)
class SectionStaffAdmin(ModelAdmin):
    list_display = ('user', 'section')
    search_fields = ('user__username', 'section__name')
    raw_id_fields = ('user',)


@admin.register(Restaurant)
class RestaurantAdmin(LeafletGeoAdminMixin, ModelAdmin):
    list_display = ('display_header', 'location', 'max_delivery_radius_km', 'show_active', 'food_count', 'created_at')
    list_filter = ('is_active', 'category', 'created_at')
    search_fields = ('name', 'location', 'owner__username', 'owner__email')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 25
    raw_id_fields = ('owner',)
    readonly_fields = ('id', 'created_at')
    inlines = [SectionInline]

    settings_overrides = {
        'DEFAULT_CENTER': (-6.7924, 39.2083),
        'DEFAULT_ZOOM': 14,
    }

    class Media:
        js = ('admin/js/geo_locate.js',)

    fieldsets = (
        (None, {'fields': ('id', 'name', 'owner', 'description', 'image')}),
        ('Location', {'fields': ('geo_location', 'location')}),
        ('Delivery', {'fields': ('max_delivery_radius_km',)}),
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
        return Food.objects.filter(section__restaurant=obj).count()

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('owner').prefetch_related('sections__foods')
