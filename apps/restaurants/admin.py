from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display
from .models import Restaurant
from apps.products.models import Food


class FoodInline(TabularInline):
    """Inline editor for foods belonging to a restaurant."""
    model = Food
    extra = 0
    fields = ('name', 'price', 'is_available')
    show_change_link = True


@admin.register(Restaurant)
class RestaurantAdmin(ModelAdmin):
    list_display = ('display_header', 'location', 'show_active', 'food_count', 'created_at')
    list_filter = ('is_active', 'category', 'created_at')
    search_fields = ('name', 'location', 'owner__username', 'owner__email')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 25
    raw_id_fields = ('owner',)
    readonly_fields = ('id', 'created_at')
    inlines = [FoodInline]

    fieldsets = (
        (None, {'fields': ('id', 'name', 'owner', 'description', 'image')}),
        ('Details', {'fields': ('location', 'category', 'price_range', 'rating', 'is_active')}),
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
