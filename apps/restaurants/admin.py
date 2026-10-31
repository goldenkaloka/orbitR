from apps.products.models import Food
from django.contrib import admin
from leaflet.admin import LeafletGeoAdminMixin
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from .models import MealPeriod, Restaurant, RestaurantSection, SectionStaff


class FoodInline(TabularInline):
    model = Food
    extra = 0
    fields = ('name', 'price', 'is_available')
    show_change_link = True


class MealPeriodInline(TabularInline):
    model = MealPeriod
    extra = 0
    fields = (
        'kind', 'label', 'start_time', 'end_time', 'preorder_cutoff_time',
        'slot_capacity', 'is_active',
    )


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


@admin.register(MealPeriod)
class MealPeriodAdmin(ModelAdmin):
    list_display = ('restaurant', 'kind', 'label', 'start_time', 'end_time', 'preorder_cutoff_time', 'slot_capacity', 'is_active')
    list_filter = ('kind', 'is_active', 'restaurant')
    search_fields = ('restaurant__name', 'label')
    raw_id_fields = ('restaurant',)


@admin.register(SectionStaff)
class SectionStaffAdmin(ModelAdmin):
    list_display = ('user', 'section')
    search_fields = ('user__username', 'section__name')
    raw_id_fields = ('user',)


@admin.register(Restaurant)
class RestaurantAdmin(LeafletGeoAdminMixin, ModelAdmin):
    list_display = ('display_header', 'source_type', 'is_verified_partner', 'dispatch_mode', 'location', 'max_delivery_radius_km', 'show_active', 'show_preorders', 'food_count', 'created_at')
    list_filter = ('source_type', 'is_verified_partner', 'dispatch_mode', 'is_active', 'preorders_enabled', 'category', 'created_at')
    search_fields = ('name', 'location', 'owner__username', 'owner__email')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 25
    raw_id_fields = ('owner',)
    readonly_fields = ('id', 'created_at')
    inlines = [MealPeriodInline, SectionInline]
    actions = ['enable_preorders', 'disable_preorders']

    settings_overrides = {
        'DEFAULT_CENTER': (-6.7924, 39.2083),
        'DEFAULT_ZOOM': 14,
    }

    class Media:
        js = ('admin/js/geo_locate.js',)

    fieldsets = (
        (None, {'fields': ('id', 'name', 'owner', 'description', 'image', 'logo')}),
        ('Operating model', {'fields': ('source_type', 'is_verified_partner', 'public_disclaimer', 'dispatch_mode')}),
        ('Location', {'fields': ('geo_location', 'location')}),
        ('Delivery', {'fields': ('max_delivery_radius_km',)}),
        ('Pre-orders', {'fields': ('preorders_enabled', 'preorder_slot_capacity')}),
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

    @display(description="Pre-orders", label=True)
    def show_preorders(self, obj):
        if obj.preorders_enabled:
            return "On", "Active"
        return "Off", "Inactive"

    @display(description="Foods")
    def food_count(self, obj):
        return Food.objects.filter(section__restaurant=obj).count()

    @admin.action(description="Enable pre-orders")
    def enable_preorders(self, request, queryset):
        self._set_preorders(request, queryset, True)

    @admin.action(description="Disable pre-orders")
    def disable_preorders(self, request, queryset):
        self._set_preorders(request, queryset, False)

    def _set_preorders(self, request, queryset, enabled):
        from api.views.core import invalidate_restaurant_cache

        count = 0
        for restaurant in queryset:
            if restaurant.preorders_enabled == enabled:
                continue
            restaurant.preorders_enabled = enabled
            restaurant.save(update_fields=['preorders_enabled'])
            invalidate_restaurant_cache(restaurant)
            count += 1
        state = 'enabled' if enabled else 'disabled'
        self.message_user(request, f"Pre-orders {state} for {count} restaurant(s).")

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        from api.views.core import invalidate_restaurant_cache

        invalidate_restaurant_cache(form.instance)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('owner').prefetch_related('sections__foods')
