from decimal import Decimal
from django.utils import timezone
from django.core.cache import cache
import logging
import h3

from apps.pricing.models import PricingPolicy, ZonePricingTier, DeliveryFeeLog, DistanceTier
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3

logger = logging.getLogger(__name__)

class PricingService:
    """
    Fully dynamic pricing engine.
    All fees computed from: distance + region + distance tier + time + season.
    No manual per-restaurant overrides — everything is formula-driven.
    """
    
    CACHE_KEY_ACTIVE_POLICY = "pricing:active_policy"
    CACHE_KEY_DISTANCE_TIERS = "pricing:distance_tiers:{policy_id}"
    CACHE_TTL = 300  # 5 minutes

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
    def get_distance_tiers(cls, policy):
        """Fetches distance tiers for a policy, cached."""
        if not policy:
            return []
        cache_key = cls.CACHE_KEY_DISTANCE_TIERS.format(policy_id=policy.pk)
        tiers = cache.get(cache_key)
        if tiers is None:
            tiers = list(DistanceTier.objects.filter(policy=policy).order_by('min_km'))
            cache.set(cache_key, tiers, cls.CACHE_TTL)
        return tiers

    @classmethod
    def get_distance_tier_multiplier(cls, tiers, distance_km):
        """
        Find the matching distance tier multiplier for a given distance.
        Falls back to 1.0 if no tiers are configured.
        """
        for tier in tiers:
            if tier.min_km <= distance_km < tier.max_km:
                return tier.multiplier
        return Decimal('1.00')

    @classmethod
    def calculate_delivery_fee(cls, restaurant, delivery_location, order=None, pool=None):
        """
        Calculates fully dynamic delivery fee.
        Returns (final_fee, service_fee, log_instance).
        """
        # 1. Guard against missing or invalid inputs
        if not delivery_location or not restaurant or not restaurant.geo_location:
            return Decimal('1000.00'), Decimal('300.00'), None

        if abs(restaurant.geo_location.x) < 0.0001 and abs(restaurant.geo_location.y) < 0.0001:
            logger.warning(f"Restaurant {restaurant.id} has invalid Point(0,0) location.")
            return Decimal('1000.00'), Decimal('300.00'), None

        try:
            # 2. H3 Indexing
            delivery_h3 = latlng_to_h3(delivery_location.y, delivery_location.x, ZONE_RESOLUTION)
            restaurant_h3 = latlng_to_h3(restaurant.geo_location.y, restaurant.geo_location.x, ZONE_RESOLUTION)
            
            if not delivery_h3 or not restaurant_h3:
                return Decimal('1000.00'), Decimal('300.00'), None

            # 3. Calculate exact distance (crow-flies)
            r_lat, r_lng = h3.cell_to_latlng(restaurant_h3)
            d_lat, d_lng = h3.cell_to_latlng(delivery_h3)
            distance_km = float(h3.great_circle_distance((r_lat, r_lng), (d_lat, d_lng)))

            # 4. Get pricing policy
            policy = cls.get_active_policy()
            service_fee = policy.service_fee if policy else Decimal('300.00')
            
            if not policy:
                return Decimal('1000.00'), service_fee, None

            # 5. Apply road buffer
            road_distance_km = distance_km * policy.road_buffer_multiplier

            # 6. Regional adjustments via OperationalZone
            from apps.geo.models import ZoneH3Index
            op_zone = None
            zone_index = ZoneH3Index.objects.filter(h3_index=delivery_h3).select_related('zone').first()
            if zone_index:
                op_zone = zone_index.zone

            zone_multiplier = Decimal('1.00')
            base_override = None
            
            if op_zone:
                try:
                    tier = op_zone.pricing_tier
                    zone_multiplier = tier.per_km_multiplier
                    base_override = tier.base_fee_override
                except ZonePricingTier.DoesNotExist:
                    pass

            # 7. Distance tier multiplier (replaces flat fee overrides)
            tiers = cls.get_distance_tiers(policy)
            distance_tier_multiplier = cls.get_distance_tier_multiplier(tiers, road_distance_km)

            # 8. Fuel economic adjustment
            fuel_adjustment = Decimal('0.00')
            if policy.fuel_price_reference and policy.fuel_price_reference > 3000:
                fuel_adjustment = (policy.fuel_price_reference - 3000) / 100

            # 9. Base calculation
            base_fee = base_override if base_override else policy.base_fee
            variable_fee = (
                Decimal(str(road_distance_km))
                * policy.per_km_rate
                * zone_multiplier
                * distance_tier_multiplier
            )
            
            final_fee = base_fee + variable_fee + fuel_adjustment

            # 10. Time & seasonal surcharges (independent, non-compounding)
            adjustments = {}
            
            if distance_tier_multiplier != Decimal('1.00'):
                adjustments['distance_tier'] = str(distance_tier_multiplier)
            
            if zone_multiplier != Decimal('1.00'):
                adjustments['zone_multiplier'] = str(zone_multiplier)

            if fuel_adjustment > 0:
                adjustments['fuel_economic_surge'] = str(fuel_adjustment.quantize(Decimal('0.01')))

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

            # 11. Round to nearest 100 TSh
            final_rounded = (final_fee / 100).quantize(Decimal('1'), rounding='ROUND_HALF_UP') * 100

            # 12. Audit log
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

    @classmethod
    def calculate_batch_fee(cls, orders, batch_type='CROSS_RESTAURANT'):
        from decimal import Decimal

        CROSS_BATCH_DISCOUNT = Decimal('0.15')

        original_total = Decimal('0')
        per_order_fees = []

        for order in orders:
            fee, service_fee, log = cls.calculate_delivery_fee(
                order.restaurant,
                order.delivery_location,
                order=order
            )
            original_total += fee
            per_order_fees.append({
                'order_id': str(order.id),
                'original_fee': fee,
                'discounted_fee': fee * (1 - CROSS_BATCH_DISCOUNT) if batch_type == 'CROSS_RESTAURANT' else fee
            })

        if batch_type == 'CROSS_RESTAURANT':
            discount_amount = original_total * CROSS_BATCH_DISCOUNT
            discounted_total = original_total - discount_amount
        else:
            discount_amount = Decimal('0')
            discounted_total = original_total

        return {
            'original_total': original_total,
            'discount_rate': CROSS_BATCH_DISCOUNT if batch_type == 'CROSS_RESTAURANT' else Decimal('0'),
            'discount_amount': discount_amount,
            'discounted_total': discounted_total,
            'per_order_fees': per_order_fees
        }

    @classmethod
    def apply_batch_fees(cls, batch):
        from apps.orders.models import Order

        orders = list(batch.orders.select_related('restaurant').all())
        if not orders:
            return None

        batch_type = batch.batch_type
        fee_result = cls.calculate_batch_fee(orders, batch_type)

        batch.original_total_fee = fee_result['original_total']
        batch.discounted_total_fee = fee_result['discounted_total']
        batch.save()

        discount_applied = batch_type == 'CROSS_RESTAURANT'
        fee_map = {fee_info['order_id']: fee_info for fee_info in fee_result['per_order_fees']}

        for order in orders:
            fee_info = fee_map.get(str(order.id))
            if fee_info:
                order.original_delivery_fee = fee_info['original_fee']
                order.delivery_fee = fee_info['discounted_fee']
                order.discount_applied = discount_applied
                order.total_amount = order.items_subtotal + order.delivery_fee + order.service_fee
                order.save()

        return fee_result
