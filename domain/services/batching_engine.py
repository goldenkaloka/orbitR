"""
BatchingEngine — entry point compatibility shim.

The actual batching logic lives in BatchPipeline
(domain/services/batch_pipeline.py).

BatchingEngine.attempt_batching(order) is the public interface called by
MarkReadyService in order_fulfillment.py. It delegates directly to
BatchPipeline.run(order) so that existing callers require no changes.

BatchingEngine.seed_strategies() is kept because Django admin tooling
may reference it to populate BatchingStrategy records.
"""
from django.core.cache import cache
from decimal import Decimal
import logging
import h3

logger = logging.getLogger(__name__)


# ─── Default strategy definitions (kept for DB seeding / admin display) ───────

DEFAULT_STRATEGIES_CONFIG = [
    {
        'name': 'SINGLE_RESTAURANT',
        'description': 'Group orders from the same restaurant in nearby H3 cells',
        'cluster_by': 'restaurant',
        'h3_resolution': 9,
        'neighbor_ring_k': 1,
        'constraints': {
            'max_orders': 5,
            'max_restaurants': 1,
            'max_distance_km': 10,
            'min_orders': 2,
        },
        'pricing': {
            'discount_rate': '0',
            'driver_payout_type': 'delivery_fee',
        },
        'route_optimization': {
            'enabled': True,
            'distance_weight': 0.6,
            'time_weight': 0.4,
            'algorithm': 'nearest_neighbor_2opt',
        },
        'priority': 20,
    },

]


# ─── Engine ───────────────────────────────────────────────────────────────────

from domain.services.mapbox_routing_service import MapboxRoutingService

class BatchingEngine:
    """
    Compatibility shim — delegates all batching to BatchPipeline.

    BatchingEngine.attempt_batching(order) is the external interface called
    by MarkReadyService (order_fulfillment.py). Keeping this class avoids
    a cascading rename across the codebase. All logic lives in BatchPipeline.
    """

    @staticmethod
    def seed_strategies():
        """Seed default strategies into the database for admin display."""
        from apps.orders.models import BatchingStrategy

        seeded = []
        for config in DEFAULT_STRATEGIES_CONFIG:
            strategy, created = BatchingStrategy.objects.update_or_create(
                name=config['name'],
                defaults=config,
            )
            seeded.append((strategy.name, created))

        logger.info(f"[BatchingEngine] Seeded {len(seeded)} batching strategies")
        return seeded

    @staticmethod
    def attempt_batching(order):
        """
        Attempt to batch a READY order.

        Delegates to BatchPipeline.run(order) — the single, unified batching
        implementation that replaces BatchingService,
        and the old BatchingEngine._process_batching().
        """
        from domain.services.batch_pipeline import BatchPipeline
        return BatchPipeline.run(order)

    @staticmethod
    def _create_single_order_batch(order):
        """Legacy unit test shim."""
        from domain.services.batch_pipeline import BatchPipeline
        
        # Call the mocked MapboxRoutingService from the patched namespace
        route_dict = None
        route_distance = Decimal('0')
        try:
            route = MapboxRoutingService.get_route_for_batch([order])
            if route:
                route_dict = route.to_dict() if hasattr(route, 'to_dict') else route
                if route_dict and route_dict.get('total_distance_km'):
                    route_distance = Decimal(str(route_dict['total_distance_km']))
        except Exception as exc:
            logger.warning(f"[BatchingEngine Shim] Mapbox failed: {exc}")
            
        return BatchPipeline._persist_single_order_batch(order.id, route_dict, route_distance)

    @staticmethod
    def _create_batch(orders, strategy, route_dict=None, is_cross=False):
        """Legacy unit test shim for multi-order batches."""
        from domain.services.batch_pipeline import BatchPipeline
        from apps.geo.models import OperationalZone
        
        # If route_dict is empty/None/list, optimize the route using the pipeline's method
        # (which calls the patched MapboxRoutingService)
        real_route = None
        if route_dict and isinstance(route_dict, dict):
            real_route = route_dict
        else:
            real_route = BatchPipeline._optimize_route(orders)
            
        # Get a zone ID for the batch
        zone_id = None
        if orders and orders[0].h3_index:
            from apps.core.geo_utils import get_zone_from_h3
            zone = get_zone_from_h3(orders[0].h3_index)
            if zone:
                zone_id = zone.id
        if not zone_id:
            zone = OperationalZone.objects.filter(is_active=True).first()
            zone_id = zone.id if zone else 1
            
        return BatchPipeline._create_multi_order_batch_atomic(orders, real_route, zone_id)