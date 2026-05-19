from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from .models import PricingPolicy, ZonePricingTier, DeliveryFeeLog, DistanceTier

class DistanceTierInline(TabularInline):
    model = DistanceTier
    extra = 1
    fields = ('min_km', 'max_km', 'multiplier')
    ordering = ('min_km',)

@admin.register(PricingPolicy)
class PricingPolicyAdmin(ModelAdmin):
    list_display = ('name', 'is_active', 'base_fee', 'per_km_rate', 'service_fee', 'updated_at')
    list_filter = ('is_active',)
    inlines = [DistanceTierInline]
    fieldsets = (
        ('Platform Strategy', {
            'fields': ('name', 'is_active')
        }),
        ('Rate Constants', {
            'fields': ('base_fee', 'per_km_rate', 'service_fee')
        }),
        ('Market Context', {
            'fields': ('fuel_price_reference',)
        }),
    )

    def save_model(self, request, obj, form, change):
        if obj.is_active:
            PricingPolicy.objects.filter(is_active=True).exclude(pk=obj.pk).update(is_active=False)
        super().save_model(request, obj, form, change)

    def update_is_active(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(request, "Select exactly one policy to activate.", level='ERROR')
            return
        policy = queryset.first()
        policy.is_active = True
        policy.save(update_fields=['is_active'])
        self.message_user(request, f"Activated {policy.name}", level='SUCCESS')
    update_is_active.short_description = "Activate selected policy"

    actions = ['update_is_active']

@admin.register(ZonePricingTier)
class ZonePricingTierAdmin(ModelAdmin):
    list_display = ('zone', 'per_km_multiplier', 'base_fee_override')
    list_filter = ('zone',)

@admin.register(DeliveryFeeLog)
class DeliveryFeeLogAdmin(ModelAdmin):
    list_display = ('calculated_at', 'restaurant', 'final_fee', 'distance_km', 'policy_name')
    readonly_fields = [f.name for f in DeliveryFeeLog._meta.fields]
    
    def has_add_permission(self, request):
        return False
    
    def has_change_permission(self, request, obj=None):
        return False
