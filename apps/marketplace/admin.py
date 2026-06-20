from django.contrib import admin
from leaflet.admin import LeafletGeoAdminMixin
from unfold.admin import ModelAdmin

from .models import MarketplaceCategory, MarketplaceProduct, ProductImage


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1

@admin.register(MarketplaceCategory)
class MarketplaceCategoryAdmin(ModelAdmin):
    list_display = ('name', 'icon')
    search_fields = ('name',)

@admin.register(MarketplaceProduct)
class MarketplaceProductAdmin(LeafletGeoAdminMixin, ModelAdmin):
    list_display = ('name', 'seller', 'price', 'status', 'listing_type', 'h3_index', 'expiry_date')
    list_filter = ('status', 'listing_type', 'category')
    search_fields = ('name', 'description', 'seller__username')
    inlines = [ProductImageInline]

    settings_overrides = {
        'DEFAULT_CENTER': (-6.7924, 39.2083),
        'DEFAULT_ZOOM': 13,
    }

    actions = ['activate_selected', 'expire_selected']

    def activate_selected(self, request, queryset):
        for product in queryset:
            product.status = 'ACTIVE'
            product.save()
    activate_selected.short_description = "Activate selected products"

    def expire_selected(self, request, queryset):
        queryset.update(status='EXPIRED')
    expire_selected.short_description = "Mark selected products as expired"
