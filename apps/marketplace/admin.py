from django.contrib import admin
from unfold.admin import ModelAdmin
from .models import MarketplaceProduct, MarketplaceCategory, ProductImage

class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1

@admin.register(MarketplaceCategory)
class MarketplaceCategoryAdmin(ModelAdmin):
    list_display = ('name', 'icon')
    search_fields = ('name',)

@admin.register(MarketplaceProduct)
class MarketplaceProductAdmin(ModelAdmin):
    list_display = ('name', 'seller', 'price', 'status', 'listing_type', 'h3_index', 'expiry_date')
    list_filter = ('status', 'listing_type', 'category')
    search_fields = ('name', 'description', 'seller__username')
    inlines = [ProductImageInline]
    
    actions = ['activate_selected', 'expire_selected']

    def activate_selected(self, request, queryset):
        for product in queryset:
            product.status = 'ACTIVE'
            product.save()
    activate_selected.short_description = "Activate selected products"

    def expire_selected(self, request, queryset):
        queryset.update(status='EXPIRED')
    expire_selected.short_description = "Mark selected products as expired"
