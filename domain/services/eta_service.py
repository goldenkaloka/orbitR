import logging
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

PREP_BASE_MINUTES = 20
TRAVEL_BASE_MINUTES = 8
KM_PER_MINUTE_PACE = 4

class ETAService:
    @staticmethod
    def calculate(order):
        if not order or not order.is_paid:
            return {
                'total_minutes': None,
                'prep_minutes': None,
                'travel_minutes': None,
                'phase': None,
                'label': None,
            }

        now = timezone.now()
        travel = ETAService._travel_time(order)

        if order.status in ('DELIVERED',):
            return {
                'total_minutes': 0,
                'prep_minutes': ETAService._prep_actual(order),
                'travel_minutes': ETAService._travel_actual(order),
                'phase': 'delivered',
                'label': 'Delivered',
            }

        if order.status in ('PENDING_PAYMENT',):
            return {
                'total_minutes': None,
                'prep_minutes': None,
                'travel_minutes': None,
                'phase': 'pending_payment',
                'label': 'Awaiting payment',
            }

        prep = ETAService._prep_time(order)

        if order.status in ('CREATED', 'CONFIRMED'):
            total = prep + travel
            return {
                'total_minutes': total,
                'prep_minutes': prep,
                'travel_minutes': travel,
                'phase': 'prepping',
                'label': f'~{total} min total • Preparing & delivery',
            }

        if order.status == 'PREPARING':
            remaining = ETAService._remaining_prep(order, prep)
            total = max(remaining, 0) + travel
            return {
                'total_minutes': total,
                'prep_minutes': max(remaining, 0),
                'travel_minutes': travel,
                'phase': 'cooking',
                'label': f'~{total} min remaining',
            }

        if order.status in ('READY',):
            total = travel
            return {
                'total_minutes': total,
                'prep_minutes': ETAService._prep_actual(order),
                'travel_minutes': travel,
                'phase': 'ready',
                'label': f'~{total} min delivery',
            }

        if order.status in ('ASSIGNED', 'PICKED'):
            return {
                'total_minutes': travel,
                'prep_minutes': ETAService._prep_actual(order),
                'travel_minutes': travel,
                'phase': 'arriving',
                'label': f'~{travel} min until arrival',
            }

        return {
            'total_minutes': prep + travel,
            'prep_minutes': prep,
            'travel_minutes': travel,
            'phase': 'unknown',
            'label': None,
        }

    @staticmethod
    def _prep_time(order):
        try:
            return order.restaurant.average_prep_time
        except Exception:
            pass
        return PREP_BASE_MINUTES

    @staticmethod
    def _remaining_prep(order, total_prep):
        if order.preparing_at:
            elapsed = (timezone.now() - order.preparing_at).total_seconds() / 60
            return int(total_prep - elapsed)
        if order.confirmed_at:
            elapsed = (timezone.now() - order.confirmed_at).total_seconds() / 60
            return max(int(total_prep - elapsed), 1)
        return total_prep

    @staticmethod
    def _travel_time(order):
        if order.delivered_at:
            return 0
        if order.driver and order.driver.location and order.delivery_location:
            try:
                from apps.core.geo_utils import haversine_distance
                dist_m = haversine_distance(order.driver.location, order.delivery_location)
                dist_km = dist_m / 1000
                driver_min = max(2, int(dist_km * KM_PER_MINUTE_PACE))
                return min(driver_min, 60)
            except Exception:
                pass
        return ETAService._static_travel_time(order)

    @staticmethod
    def _static_travel_time(order):
        if not order.restaurant or not order.delivery_location or not order.restaurant.geo_location:
            return TRAVEL_BASE_MINUTES
        try:
            dist_km = order.restaurant.geo_location.distance(order.delivery_location) / 1000
            est = TRAVEL_BASE_MINUTES + (dist_km * KM_PER_MINUTE_PACE)
            return int(min(60, est))
        except Exception:
            return TRAVEL_BASE_MINUTES

    @staticmethod
    def _prep_actual(order):
        start = order.confirmed_at or order.created_at
        end = order.ready_at or order.picked_up_at or timezone.now()
        if start:
            elapsed = (end - start).total_seconds() / 60
            return int(elapsed)
        return None

    @staticmethod
    def _travel_actual(order):
        if order.picked_up_at and order.delivered_at:
            elapsed = (order.delivered_at - order.picked_up_at).total_seconds() / 60
            return int(elapsed)
        return None
