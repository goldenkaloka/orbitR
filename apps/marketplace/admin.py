from django.contrib import admin
from leaflet.admin import LeafletGeoAdminMixin
from unfold.admin import ModelAdmin

from .models import (
    MarketplaceCategory, MarketplaceProduct, ProductImage,
    PostingTokenConfig, PostingTokenBundleOption, PostingTokenBundlePurchase, PostingTokenTransaction
)


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


@admin.register(PostingTokenConfig)
class PostingTokenConfigAdmin(ModelAdmin):
    list_display = ('free_tokens_for_new_users', 'token_deduction_per_listing')


@admin.register(PostingTokenBundleOption)
class PostingTokenBundleOptionAdmin(ModelAdmin):
    list_display = ('id', 'name', 'price', 'tokens_granted', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('name',)


@admin.register(PostingTokenBundlePurchase)
class PostingTokenBundlePurchaseAdmin(ModelAdmin):
    list_display = ('user', 'bundle', 'amount', 'tokens_granted', 'is_paid', 'created_at')
    list_filter = ('is_paid', 'bundle')
    search_fields = ('user__username', 'bundle__name')


@admin.register(PostingTokenTransaction)
class PostingTokenTransactionAdmin(ModelAdmin):
    list_display = ('user', 'transaction_type', 'amount', 'balance_after', 'created_at')
    list_filter = ('transaction_type',)
    search_fields = ('user__username', 'description')
