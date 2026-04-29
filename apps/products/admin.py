from django.contrib import admin
from unfold.admin import ModelAdmin
from unfold.decorators import display
from .models import Food


@admin.register(Food)
class FoodAdmin(ModelAdmin):
    list_display = ('name', 'vendor', 'price', 'show_availability')
    list_filter = ('is_available', 'vendor')
    search_fields = ('name', 'vendor__name')
    ordering = ('vendor', 'name')
    list_per_page = 25
    list_editable = ('price',)
    raw_id_fields = ('vendor',)
    readonly_fields = ('id',)

    fieldsets = (
        (None, {'fields': ('id', 'name', 'vendor', 'description', 'image')}),
        ('Pricing & Availability', {'fields': ('price', 'is_available')}),
    )

    @display(
        description="Availability",
        label={
            "Available": "success",
            "Out of Stock": "danger",
        },
    )
    def show_availability(self, obj):
        return "Available" if obj.is_available else "Out of Stock"

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('vendor')
