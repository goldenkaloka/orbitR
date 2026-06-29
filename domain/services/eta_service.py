import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

PREP_BASE_MINUTES = 20
TRAVEL_BASE_MINUTES = 8
ROAD_DISTANCE_FACTOR = 1.35
AVERAGE_CITY_SPEED_KMH = 24
PICKUP_BUFFER_MINUTES = 4
DROPOFF_BUFFER_MINUTES = 2
MAX_ETA_MINUTES = 180
MAX_TRAVEL_MINUTES = 90


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
            total = ETAService._clamp_eta(prep + travel)
            return {
                'total_minutes': total,
                'prep_minutes': prep,
                'travel_minutes': travel,
                'phase': 'prepping',
                'label': f'~{total} min total • Preparing & delivery',
            }

        if order.status == 'PREPARING':
            remaining = max(ETAService._remaining_prep(order, prep), 0)
            total = ETAService._clamp_eta(remaining + travel)
            return {
                'total_minutes': total,
                'prep_minutes': remaining,
                'travel_minutes': travel,
                'phase': 'cooking',
                'label': f'~{total} min remaining',
            }

        if order.status in ('READY',):
            total = ETAService._clamp_eta(travel)
            return {
                'total_minutes': total,
                'prep_minutes': ETAService._prep_actual(order),
                'travel_minutes': travel,
                'phase': 'ready',
                'label': f'~{total} min delivery',
            }

        if order.status in ('ASSIGNED', 'PICKED'):
            total = ETAService._clamp_eta(travel)
            return {
                'total_minutes': total,
                'prep_minutes': ETAService._prep_actual(order),
                'travel_minutes': travel,
                'phase': 'arriving',
                'label': f'~{total} min until arrival',
            }

        total = ETAService._clamp_eta(prep + travel)
        return {
            'total_minutes': total,
            'prep_minutes': prep,
            'travel_minutes': travel,
            'phase': 'unknown',
            'label': f'~{total} min',
        }

    @staticmethod
    def _prep_time(order):
        section_minutes = ETAService._section_prep_time(order)
        if section_minutes:
            return section_minutes
        try:
            return order.restaurant.average_prep_time
        except Exception:
            return PREP_BASE_MINUTES

    @staticmethod
    def _remaining_prep(order, total_prep):
        if order.ready_at:
            return 0
        batch_remaining = ETAService._batch_section_remaining(order)
        if batch_remaining is not None:
            return batch_remaining
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

        route_minutes = ETAService._route_optimization_minutes(order)
        if route_minutes is not None:
            return route_minutes

        if order.status == 'PICKED' and order.driver and order.driver.location and order.delivery_location:
            return ETAService._point_to_point_minutes(
                order.driver.location,
                order.delivery_location,
                include_dropoff=True,
            )

        if order.status == 'ASSIGNED' and order.driver and order.driver.location and order.restaurant and order.restaurant.geo_location:
            to_pickup = ETAService._point_to_point_minutes(order.driver.location, order.restaurant.geo_location)
            to_dropoff = ETAService._point_to_point_minutes(
                order.restaurant.geo_location,
                order.delivery_location,
                include_dropoff=True,
            )
            return ETAService._clamp_travel(to_pickup + PICKUP_BUFFER_MINUTES + to_dropoff)

        return ETAService._static_travel_time(order)

    @staticmethod
    def _static_travel_time(order):
        if not order.restaurant or not order.delivery_location or not order.restaurant.geo_location:
            return TRAVEL_BASE_MINUTES
        return ETAService._point_to_point_minutes(
            order.restaurant.geo_location,
            order.delivery_location,
            include_dropoff=True,
        )

    @staticmethod
    def _point_to_point_minutes(origin, destination, include_dropoff=False):
        if not origin or not destination:
            return TRAVEL_BASE_MINUTES
        try:
            from apps.core.geo_utils import haversine_distance
            dist_km = (haversine_distance(origin, destination) / 1000) * ROAD_DISTANCE_FACTOR
            travel_minutes = (dist_km / AVERAGE_CITY_SPEED_KMH) * 60
            if include_dropoff:
                travel_minutes += DROPOFF_BUFFER_MINUTES
            return ETAService._clamp_travel(max(TRAVEL_BASE_MINUTES, travel_minutes))
        except Exception:
            return TRAVEL_BASE_MINUTES

    @staticmethod
    def _route_optimization_minutes(order):
        route = getattr(getattr(order, 'batch', None), 'route_optimization', None)
        if not isinstance(route, dict):
            return None
        stops = route.get('stop_sequence')
        if not stops:
            return None
        for stop in stops:
            if str(stop.get('order_id')) == str(order.id) and stop.get('type') == 'delivery':
                try:
                    return ETAService._clamp_travel(int(stop.get('estimated_arrival_min')) + DROPOFF_BUFFER_MINUTES)
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _section_prep_time(order):
        try:
            items = ETAService._order_items(order, include_sections=True)
            section_times = [
                item.section.prep_time
                for item in items
                if item.section and item.section.prep_time
            ]
            return max(section_times) if section_times else None
        except Exception:
            return None

    @staticmethod
    def _batch_section_remaining(order):
        if not getattr(order, 'batch_id', None):
            return None
        try:
            section_ids = [item.section_id for item in ETAService._order_items(order) if item.section_id]
            if not section_ids:
                return None
            batch_cache = getattr(order.batch, '_prefetched_objects_cache', {})
            if 'section_statuses' in batch_cache:
                statuses = [
                    status for status in order.batch.section_statuses.all()
                    if (
                        status.section_id in section_ids
                        and status.estimated_ready_at is not None
                        and status.status != 'READY'
                    )
                ]
            else:
                statuses = order.batch.section_statuses.filter(
                    section_id__in=section_ids,
                    estimated_ready_at__isnull=False,
                ).exclude(status='READY')
            remaining = [
                int((status.estimated_ready_at - timezone.now()).total_seconds() / 60)
                for status in statuses
            ]
            return max(remaining) if remaining else None
        except Exception:
            return None

    @staticmethod
    def _order_items(order, include_sections=False):
        cache = getattr(order, '_prefetched_objects_cache', {})
        if 'items' in cache:
            return list(order.items.all())
        queryset = order.items.all()
        if include_sections:
            queryset = queryset.select_related('section')
        return list(queryset)

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

    @staticmethod
    def _clamp_travel(value):
        return int(min(MAX_TRAVEL_MINUTES, max(1, round(value))))

    @staticmethod
    def _clamp_eta(value):
        return int(min(MAX_ETA_MINUTES, max(1, round(value))))
