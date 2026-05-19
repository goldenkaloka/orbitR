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
    {
        'name': 'CROSS_RESTAURANT',
        'description': 'Group orders from different restaurants with nearby delivery locations',
        'cluster_by': 'delivery_h3',
        'h3_resolution': 10,
        'neighbor_ring_k': 2,
        'constraints': {
            'max_orders': 5,
            'max_restaurants': 3,
            'max_distance_km': 8,
            'min_orders': 2,
            'min_restaurants': 2,
        },
        'pricing': {
            'discount_rate': '0.15',
            'driver_payout_type': 'delivery_fee',
        },
        'route_optimization': {
            'enabled': True,
            'distance_weight': 0.6,
            'time_weight': 0.4,
            'algorithm': 'nearest_neighbor_2opt',
        },
        'priority': 10,
    },
    {
        'name': 'TIME_WINDOW',
        'description': 'Group orders ready within a time window',
        'cluster_by': 'time_window',
        'h3_resolution': 9,
        'neighbor_ring_k': 1,
        'constraints': {
            'max_orders': 5,
            'max_restaurants': 3,
            'max_distance_km': 12,
            'min_orders': 2,
            'time_window_min': 15,
        },
        'pricing': {
            'discount_rate': '0.10',
            'driver_payout_type': 'delivery_fee',
        },
        'route_optimization': {
            'enabled': True,
            'distance_weight': 0.6,
            'time_weight': 0.4,
            'algorithm': 'nearest_neighbor_2opt',
        },
        'priority': 5,
    },
]


# ─── Engine ───────────────────────────────────────────────────────────────────

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
        implementation that replaces BatchingService, CrossBatchingService,
        and the old BatchingEngine._process_batching().
        """
        from domain.services.batch_pipeline import BatchPipeline
        return BatchPipeline.run(order)