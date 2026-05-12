from decimal import Decimal
from django.utils import timezone
from django.core.cache import cache
import logging
import h3

from apps.restaurants.models import DeliveryZone
from apps.geo.models import OperationalZone
from apps.pricing.models import PricingPolicy, ZonePricingTier, DeliveryFeeLog
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3

logger = logging.getLogger(__name__)

class PricingService:
    """
    Advanced Pricing Engine for Dynamic Delivery Fees.
    Integrates H3 spatial indexing, regional pricing tiers, and transport cost factors.
    """
    
    CACHE_KEY_ACTIVE_POLICY = "pricing:active_policy"
    CACHE_TTL = 300 # 5 minutes

    @classmethod
    def get_active_policy(cls):
        """Fetches active policy from cache or DB."""
        policy = cache.get(cls.CACHE_KEY_ACTIVE_POLICY)
        if not policy:
            policy = PricingPolicy.objects.filter(is_active=True).first()
            if policy:
                cache.set(cls.CACHE_KEY_ACTIVE_POLICY, policy, cls.CACHE_TTL)
        return policy

    @classmethod
    def calculate_delivery_fee(cls, restaurant, delivery_location, order=None, pool=None):
        """
        Calculates dynamic delivery fee and returns (final_fee, log_instance).
        """
        # 1. Enhanced Guard against missing or invalid inputs
        if not delivery_location or not restaurant or not restaurant.geo_location:
            return Decimal('1000.00'), Decimal('300.00'), None

        # Guard against Point(0,0) which is often a default for uninitialized locations
        if abs(restaurant.geo_location.x) < 0.0001 and abs(restaurant.geo_location.y) < 0.0001:
            logger.warning(f"Restaurant {restaurant.id} has invalid Point(0,0) location.")
            return Decimal('1000.00'), Decimal('300.00'), None

        try:
            # 2. H3 Indexing with Guards
            delivery_h3 = latlng_to_h3(delivery_location.y, delivery_location.x, ZONE_RESOLUTION)
            restaurant_h3 = latlng_to_h3(restaurant.geo_location.y, restaurant.geo_location.x, ZONE_RESOLUTION)
            
            if not delivery_h3 or not restaurant_h3:
                return Decimal('1000.00'), Decimal('300.00'), None

            # Calculate Exact Distance (Crow-flies)
            r_lat, r_lng = h3.cell_to_latlng(restaurant_h3)
            d_lat, d_lng = h3.cell_to_latlng(delivery_h3)
            distance_km = float(h3.great_circle_distance((r_lat, r_lng), (d_lat, d_lng)))
            
            # 3. Check for Restaurant-Specific Delivery Zone
            zone = DeliveryZone.objects.filter(
                restaurant=restaurant,
                is_active=True,
                h3_index=delivery_h3
            ).order_by('-priority').first()

            if zone:
                policy = cls.get_active_policy()
                s_fee = policy.service_fee if policy else Decimal('300.00')
                log = DeliveryFeeLog.objects.create(
                    order=order,
                    pool=pool,
                    restaurant=restaurant,
                    distance_km=distance_km,
                    estimated_road_distance_km=distance_km,
                    base_fee_used=zone.fee,
                    per_km_rate_used=Decimal('0.00'),
                    final_fee=zone.fee,
                    adjustments={'restaurant_zone_override': zone.name},
                    policy_name="RESTAURANT_OVERRIDE"
                )
                return zone.fee, s_fee, log

            # 4. Dynamic Calculation via Pricing Policy
            policy = cls.get_active_policy()
            service_fee = policy.service_fee if policy else Decimal('300.00')
            
            if not policy:
                return Decimal('1000.00'), service_fee, None

            # Apply Road Buffer
            road_distance_km = distance_km * policy.road_buffer_multiplier
            
            # 5. Regional Adjustments - OPTIMIZED: Using ZoneH3Index instead of boundary__contains
            from apps.geo.models import ZoneH3Index
            op_zone = None
            zone_index = ZoneH3Index.objects.filter(h3_index=delivery_h3).select_related('zone').first()
            if zone_index:
                op_zone = zone_index.zone

            multiplier = Decimal('1.00')
            base_override = None
            
            if op_zone:
                try:
                    tier = op_zone.pricing_tier
                    multiplier = tier.per_km_multiplier
                    base_override = tier.base_fee_override
                except ZonePricingTier.DoesNotExist:
                    pass

            # 6. Base Calculation
            # Integrate fuel_price_reference: If fuel is expensive (> 3000 TSh/L), add a small economic surcharge
            fuel_adjustment = Decimal('0.00')
            if policy.fuel_price_reference and policy.fuel_price_reference > 3000:
                fuel_adjustment = (policy.fuel_price_reference - 3000) / 100 # e.g. 500 TSh if fuel is 3500
            
            base_fee = base_override if base_override else policy.base_fee
            variable_fee = Decimal(str(road_distance_km)) * policy.per_km_rate * multiplier
            
            # Use a separate variable for final accumulation to prevent compounding
            final_fee = base_fee + variable_fee + fuel_adjustment
            
            # 7. Surcharges (Independent, non-compounding)
            adjustments = {}
            if fuel_adjustment > 0:
                adjustments['fuel_economic_surge'] = str(fuel_adjustment.quantize(Decimal('0.01')))

            # Use Local Time (Tanzania UTC+3)
            now_local = timezone.localtime(timezone.now())
            hour = now_local.hour
            month = now_local.month
            
            # Rush Hour (7-9 AM, 4-7 PM)
            if (7 <= hour <= 9) or (16 <= hour <= 19):
                rush_adj = (base_fee + variable_fee) * (policy.rush_hour_multiplier - Decimal('1.0'))
                final_fee += rush_adj
                adjustments['rush_hour'] = str(rush_adj.quantize(Decimal('0.01')))

            # Night Surcharge (10 PM - 6 AM)
            elif hour >= 22 or hour <= 6:
                night_adj = (base_fee + variable_fee) * (policy.night_surcharge_multiplier - Decimal('1.0'))
                final_fee += night_adj
                adjustments['night'] = str(night_adj.quantize(Decimal('0.01')))

            # Rainy Season (March-May, Oct-Dec)
            is_rainy_season = (3 <= month <= 5) or (10 <= month <= 12)
            if is_rainy_season and policy.rainy_season_surcharge > 0:
                final_fee += policy.rainy_season_surcharge
                adjustments['rainy_season'] = str(policy.rainy_season_surcharge)

            # 8. Final Polish
            # Round to nearest 100 TSh
            final_rounded = (final_fee / 100).quantize(Decimal('1'), rounding='ROUND_HALF_UP') * 100
            
            # 9. Audit Logging
            log = DeliveryFeeLog.objects.create(
                order=order,
                pool=pool,
                restaurant=restaurant,
                distance_km=distance_km,
                estimated_road_distance_km=road_distance_km,
                base_fee_used=base_fee,
                per_km_rate_used=policy.per_km_rate,
                final_fee=final_rounded,
                adjustments=adjustments,
                policy_name=policy.name
            )

            return final_rounded, service_fee, log

        except Exception as e:
            logger.error(f"Error calculating delivery fee: {str(e)}")
            return Decimal('1000.00'), Decimal('300.00'), None
