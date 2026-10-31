"""
Single source of truth for what a rider is paid for a trip.

Rider pay used to be computed in four places — batch finalize, pool batch
creation, delivery completion and payment release — with fallback constants
(200 / 50 / 30) that no longer matched the pricing model defaults. Every
caller now asks this service instead.
"""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

CENT = Decimal('0.01')

# Mirrors apps.pricing.models.PricingPolicy defaults. Only used when no
# policy row can be loaded, which on a live database does not happen.
_DEFAULTS = {
    'driver_min_guarantee': Decimal('1500.00'),
    'driver_per_km_rate': Decimal('500.00'),
    'driver_per_stop_rate': Decimal('300.00'),
    'single_order_driver_share': Decimal('0.95'),
    'drop_trip_payout': Decimal('3000.00'),
}


class PayoutService:
    @staticmethod
    def rates() -> dict:
        from domain.services.pricing_service import PricingService

        policy = PricingService.get_active_policy()
        rates = {}
        for field, default in _DEFAULTS.items():
            value = getattr(policy, field, None) if policy else None
            rates[field] = Decimal(str(value)) if value is not None else default
        return rates

    @staticmethod
    def is_drop_trip(orders) -> bool:
        return bool(orders) and all(getattr(o, 'drop_window_id', None) for o in orders)

    @classmethod
    def quote(cls, orders, route_dict=None, user_paid_total=None,
              stops=None) -> tuple:
        """Pay for one trip: (driver_payout, user_paid_total).

        Drop trips pay the flat trip fee, deliberately uncapped — that flat fee
        is what makes a 500 fee per drop order viable instead of eight
        guarantee-sized payouts. Every other trip pays the guarantee formula
        over the routed stops, or the single-order share of delivery fees when
        there is no route, capped at what was collected. Pass `stops` when the
        trip's physical stops are known independently of the route (a pool is
        one pickup and one drop, however many members pay for it).
        """
        rates = cls.rates()
        orders = list(orders)

        total_delivery_fees = sum(Decimal(str(o.delivery_fee or 0)) for o in orders)
        total_service_fees = sum(Decimal(str(o.service_fee or 0)) for o in orders)
        if user_paid_total is None:
            user_paid_total = total_delivery_fees + total_service_fees
        user_paid_total = Decimal(str(user_paid_total))

        if cls.is_drop_trip(orders):
            return rates['drop_trip_payout'].quantize(CENT), user_paid_total.quantize(CENT)

        has_route = bool(route_dict and route_dict.get('total_distance_km'))
        if stops is None:
            stops = len(route_dict.get('stop_sequence') or []) if has_route else 0

        if has_route:
            distance = Decimal(str(route_dict['total_distance_km']))
            payout = (
                rates['driver_min_guarantee']
                + distance * rates['driver_per_km_rate']
                + Decimal(stops) * rates['driver_per_stop_rate']
            )
        else:
            payout = max(
                rates['driver_min_guarantee'],
                total_delivery_fees * rates['single_order_driver_share'],
            )

        if user_paid_total > 0:
            capped = min(payout, user_paid_total)
            if capped != payout:
                logger.warning(
                    "[Payout] Capping rider payout %s to collected %s for %s order(s)",
                    payout, user_paid_total, len(orders),
                )
            payout = capped
        payout = max(payout, Decimal('0'))
        return payout.quantize(CENT), user_paid_total.quantize(CENT)

    @classmethod
    def order_share(cls, order) -> Decimal:
        """This order's slice of its trip's payout; 0 when there is no trip."""
        batch = getattr(order, 'batch', None)
        if batch is not None and batch.driver_payout:
            count = batch.orders.count()
            if count > 0:
                return (Decimal(str(batch.driver_payout)) / count).quantize(CENT)

        if order.driver_id is None and getattr(order, 'driver', None) is None:
            return Decimal('0')

        share = cls.rates()['single_order_driver_share']
        return (Decimal(str(order.delivery_fee or 0)) * share).quantize(CENT)
