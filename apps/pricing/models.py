from django.db import models
from django.utils import timezone
from decimal import Decimal

class PricingPolicy(models.Model):
    """
    Centralized pricing strategy for the platform.
    Allows business managers to adjust fees without code changes.
    """
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=False, help_text="Only one policy can be active at a time")
    
    # Base rates
    base_fee = models.DecimalField(max_digits=10, decimal_places=2, default=1000.00, help_text="Minimum starting fee in TSh")
    per_km_rate = models.DecimalField(max_digits=10, decimal_places=2, default=500.00, help_text="Variable rate per kilometer")
    service_fee = models.DecimalField(max_digits=10, decimal_places=2, default=300.00, help_text="Flat platform service fee")
    road_buffer_multiplier = models.FloatField(default=1.3, help_text="Multiplies crow-flies distance to estimate actual road distance")
    
    # Global Surcharges
    rainy_season_surcharge = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    night_surcharge_multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=1.20, help_text="1.20 = 20% extra")
    rush_hour_multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=1.15)

    # Reference Economics
    fuel_price_reference = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="For tracking why this policy was created")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        from django.core.cache import cache
        if self.is_active:
            # Deactivate other policies if this one is set to active
            PricingPolicy.objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)
        
        # Invalidate pricing cache
        cache.delete("pricing:active_policy")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} {'(ACTIVE)' if self.is_active else ''}"

    class Meta:
        verbose_name_plural = "Pricing Policies"

class ZonePricingTier(models.Model):
    """
    Links OperationalZones to specific pricing multipliers.
    Used for urban/rural or hotspot price adjustments.
    """
    zone = models.OneToOneField('geo.OperationalZone', on_delete=models.CASCADE, related_name='pricing_tier')
    per_km_multiplier = models.DecimalField(max_digits=5, decimal_places=2, default=1.00, help_text="e.g. 0.8 for high-density areas, 1.5 for rural")
    base_fee_override = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"{self.zone.name} - {self.per_km_multiplier}x"

class DeliveryFeeLog(models.Model):
    """
    Audit trail for transparency and debugging of delivery fees.
    """
    order = models.OneToOneField('orders.Order', on_delete=models.CASCADE, related_name='fee_log', null=True, blank=True)
    pool = models.OneToOneField('orders.OrderPool', on_delete=models.CASCADE, related_name='fee_log', null=True, blank=True)
    restaurant = models.ForeignKey('restaurants.Restaurant', on_delete=models.SET_NULL, null=True)
    
    distance_km = models.FloatField()
    estimated_road_distance_km = models.FloatField()
    
    base_fee_used = models.DecimalField(max_digits=10, decimal_places=2)
    per_km_rate_used = models.DecimalField(max_digits=10, decimal_places=2)
    
    final_fee = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Store adjustments as JSON for flexibility
    # e.g. {"rush_hour": 200, "night": 300, "zone_multiplier": 0.8}
    adjustments = models.JSONField(default=dict)
    
    # Snapshot of policy name
    policy_name = models.CharField(max_length=100, default='UNKNOWN')
    
    calculated_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"Fee Log: {self.final_fee} TSh"
