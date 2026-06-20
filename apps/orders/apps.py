import logging

from django.apps import AppConfig

logger = logging.getLogger(__name__)

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


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.orders'

    def ready(self):
        pass
