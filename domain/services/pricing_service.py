import logging
from decimal import Decimal
from types import SimpleNamespace

import h3
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3
from apps.pricing.models import (
    DeliveryFeeLog,
    DistanceTier,
    PricingPolicy,
    ZonePricingTier,
)
from django.core.cache import cache
from django.forms.models import model_to_dict
from django.utils import timezone

logger = logging.getLogger(__name__)


def _policy_from_dict(data):
    """Reconstruct a policy-like object from a serialized dict."""
    if data is None:
        return None
    obj = SimpleNamespace(**data)
    obj.pk = data.get('id')
    return obj


def _policy_to_dict(policy):
    """Serialize a PricingPolicy to a plain dict for cache."""
    d = model_to_dict(policy)
    d['id'] = policy.pk
    return d


def _tier_from_dict(data):
    """Reconstruct a distance tier-like object from a serialized dict."""
    if data is None:
        return None
    return SimpleNamespace(**data)


def _tier_to_dict(tier):
    """Serialize a DistanceTier to a plain dict for cache."""
    return model_to_dict(tier)


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
        """Fetches active policy from cache or DB. Creates default if none exists."""
        cached = cache.get(cls.CACHE_KEY_ACTIVE_POLICY)
        if cached is not None:
            return _policy_from_dict(cached)
        policy = PricingPolicy.objects.filter(is_active=True).first()
        if not policy:
            policy = cls._ensure_default_policy()
        if policy:
            cache.set(cls.CACHE_KEY_ACTIVE_POLICY, _policy_to_dict(policy), cls.CACHE_TTL)
        return policy

    @classmethod
    def _ensure_default_policy(cls):
        """Ensure a default pricing policy exists and is active."""
        from decimal import Decimal
        policy = PricingPolicy.objects.filter(name='Default Policy').first()
        if policy:
            if not policy.is_active:
                policy.is_active = True
                policy.save(update_fields=['is_active'])
            return policy

        policy = PricingPolicy.objects.create(
            name='Default Policy',
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            service_fee=Decimal('300.00'),
            driver_min_guarantee=Decimal('2000.00'),
            driver_per_km_rate=Decimal('500.00'),
            driver_per_stop_rate=Decimal('300.00'),
            single_order_driver_share=Decimal('0.90')
        )
        return policy

    @classmethod
    def get_distance_tiers(cls, policy):
        """Fetches distance tiers for a policy, cached."""
        if not policy:
            return []
        cache_key = cls.CACHE_KEY_DISTANCE_TIERS.format(policy_id=policy.pk)
        cached = cache.get(cache_key)
        if cached is not None:
            return [_tier_from_dict(d) for d in cached]
        tiers = list(DistanceTier.objects.filter(policy=policy).order_by('min_km'))
        cache.set(cache_key, [_tier_to_dict(t) for t in tiers], cls.CACHE_TTL)
        return tiers

    @classmethod
    def get_distance_tier_multiplier(cls, tiers, distance_km):
        """
        Find the matching distance tier multiplier for a given distance.
        Falls back to 1.0 if no tiers are configured.
        If the distance exceeds the highest tier, use the highest tier's multiplier.
        """
        tiers_list = list(tiers)
        if not tiers_list:
            return Decimal('1.00')
        for tier in tiers_list:
            if tier.min_km <= distance_km < tier.max_km:
                return tier.multiplier
        highest_tier = tiers_list[-1]
        if distance_km >= highest_tier.max_km:
            return highest_tier.multiplier
        return Decimal('1.00')

    @classmethod
    def calculate_delivery_fee(cls, restaurant, delivery_location, order=None, pool=None, skip_log=False):
        """
        Calculates fully dynamic delivery fee.
        Returns (final_fee, service_fee, log_instance).
        Set skip_log=True to skip database log creation (for batch recalculation).
        """
        if not delivery_location:
            logger.warning("PRICING FALLBACK: delivery_location is None")
            return Decimal('1000.00'), Decimal('300.00'), None

        if not restaurant:
            logger.warning("PRICING FALLBACK: restaurant is None")
            return Decimal('1000.00'), Decimal('300.00'), None

        if not restaurant.geo_location:
            logger.warning(f"PRICING FALLBACK: Restaurant {restaurant.id} has no geo_location")
            return Decimal('1000.00'), Decimal('300.00'), None

        if abs(restaurant.geo_location.x) < 0.0001 and abs(restaurant.geo_location.y) < 0.0001:
            logger.warning(f"PRICING FALLBACK: Restaurant {restaurant.id} has invalid Point(0,0) location.")
            return Decimal('1000.00'), Decimal('300.00'), None

        try:
            delivery_h3 = latlng_to_h3(delivery_location.y, delivery_location.x, ZONE_RESOLUTION)
            restaurant_h3 = latlng_to_h3(restaurant.geo_location.y, restaurant.geo_location.x, ZONE_RESOLUTION)

            if not delivery_h3:
                logger.warning(f"PRICING FALLBACK: Failed to compute H3 for delivery location ({delivery_location.y}, {delivery_location.x})")
                return Decimal('1000.00'), Decimal('300.00'), None

            if not restaurant_h3:
                logger.warning(f"PRICING FALLBACK: Failed to compute H3 for restaurant location ({restaurant.geo_location.y}, {restaurant.geo_location.x})")
                return Decimal('1000.00'), Decimal('300.00'), None

            r_lat, r_lng = h3.cell_to_latlng(restaurant_h3)
            d_lat, d_lng = h3.cell_to_latlng(delivery_h3)
            distance_km = float(h3.great_circle_distance((r_lat, r_lng), (d_lat, d_lng)))

            policy = cls.get_active_policy()
            service_fee = policy.service_fee if policy else Decimal('300.00')

            if not policy:
                logger.warning("PRICING FALLBACK: No active PricingPolicy found in database")
                return Decimal('1000.00'), service_fee, None

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

            tiers = cls.get_distance_tiers(policy)
            distance_tier_multiplier = cls.get_distance_tier_multiplier(tiers, distance_km)

            fuel_adjustment = Decimal('0.00')
            if policy.fuel_price_reference and policy.fuel_price_reference > 3000:
                fuel_adjustment = (policy.fuel_price_reference - 3000) / 100

            base_fee = base_override if base_override else policy.base_fee
            variable_fee = (
                Decimal(str(distance_km))
                * policy.per_km_rate
                * zone_multiplier
                * distance_tier_multiplier
            )

            final_fee = base_fee + variable_fee + fuel_adjustment

            # Limitless delivery fee cap to protect users against excessive rates
            MAX_DELIVERY_FEE = Decimal('25000.00')
            adjustments = {}

            if final_fee > MAX_DELIVERY_FEE:
                final_fee = MAX_DELIVERY_FEE
                adjustments['cap_applied'] = 'true'

            if distance_tier_multiplier != Decimal('1.00'):
                adjustments['distance_tier'] = str(distance_tier_multiplier)
            
            if zone_multiplier != Decimal('1.00'):
                adjustments['zone_multiplier'] = str(zone_multiplier)

            if fuel_adjustment > 0:
                adjustments['fuel_economic_surge'] = str(fuel_adjustment.quantize(Decimal('0.01')))

            final_rounded = (final_fee / 100).quantize(Decimal('1'), rounding='ROUND_HALF_UP') * 100

            log_data = {
                'order': order,
                'pool': pool,
                'restaurant': restaurant,
                'distance_km': distance_km,
                'estimated_road_distance_km': distance_km,
                'base_fee_used': base_fee,
                'per_km_rate_used': policy.per_km_rate,
                'final_fee': final_rounded,
                'adjustments': adjustments,
                'policy_name': policy.name,
            }
            log = DeliveryFeeLog(**log_data) if skip_log else DeliveryFeeLog.objects.create(**log_data)

            return final_rounded, service_fee, log

        except Exception as e:
            logger.error(f"PRICING FALLBACK: Exception during calculation: {str(e)}", exc_info=True)
            return Decimal('1000.00'), Decimal('300.00'), None

    @classmethod
    def calculate_batch_fee(cls, orders, batch_type='SINGLE_ORDER'):
        original_total = Decimal('0')
        per_order_fees = []

        for order in orders:
            if getattr(order, 'delivery_fee', None) is not None:
                fee = order.delivery_fee
            else:
                fee, service_fee, log = cls.calculate_delivery_fee(
                    order.restaurant,
                    order.delivery_location,
                    order=order
                )
            original_total += fee
            per_order_fees.append({
                'order_id': str(order.id),
                'original_fee': fee,
                'discounted_fee': fee,
            })

        return {
            'original_total': original_total,
            'discount_rate': Decimal('0'),
            'discount_amount': Decimal('0'),
            'discounted_total': original_total,
            'per_order_fees': per_order_fees
        }

    @classmethod
    def apply_batch_fees(cls, batch):
        from apps.orders.models import Order
        from django.db import transaction

        orders = list(batch.orders.select_related('restaurant').all())
        if not orders:
            return None

        fee_result = cls.calculate_batch_fee(orders)

        discount_applied = False
        fee_map = {fee_info['order_id']: fee_info for fee_info in fee_result['per_order_fees']}

        with transaction.atomic():
            batch.original_total_fee = fee_result['original_total']
            batch.discounted_total_fee = fee_result['discounted_total']
            batch.save()

            for order in orders:
                fee_info = fee_map.get(str(order.id))
                if fee_info:
                    order.original_delivery_fee = fee_info['original_fee']
                    order.delivery_fee = fee_info['discounted_fee']
                    order.discount_applied = discount_applied
                    order.total_amount = order.items_subtotal + order.delivery_fee + order.service_fee

            Order.objects.bulk_update(
                orders,
                ['original_delivery_fee', 'delivery_fee', 'discount_applied', 'total_amount']
            )

        return fee_result
