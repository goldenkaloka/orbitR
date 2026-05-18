from django.db import transaction
from django.core.cache import cache
from decimal import Decimal
import logging
import h3

logger = logging.getLogger(__name__)

BATCHING_LOCK_TTL = 30


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


class BatchingEngine:
    """Unified batching engine driven by strategy configs"""

    @staticmethod
    def seed_strategies():
        """Seed default strategies into database"""
        from apps.orders.models import BatchingStrategy

        created = []
        for config in DEFAULT_STRATEGIES_CONFIG:
            strategy, created_flag = BatchingStrategy.objects.update_or_create(
                name=config['name'],
                defaults=config
            )
            created.append((strategy.name, created_flag))

        logger.info(f"Seeded {len(created)} batching strategies")
        return created

    @staticmethod
    def attempt_batching(order):
        """Main entry point for batching an order"""
        if not order.h3_index:
            logger.debug(f"Order {order.id} has no h3_index, skipping batching")
            return None

        lock_key = f"batching_lock:{order.h3_index[:8]}"
        lock_acquired = cache.add(lock_key, "1", BATCHING_LOCK_TTL)

        if not lock_acquired:
            logger.debug(f"Another worker processing batching for H3 {order.h3_index[:8]}")
            return None

        try:
            return BatchingEngine._process_batching(order)
        except Exception as e:
            logger.error(f"Batching failed for order {order.id}: {str(e)}")
            return None
        finally:
            cache.delete(lock_key)

    @staticmethod
    def _process_batching(order):
        from apps.orders.models import BatchingStrategy, Order, DeliveryBatch, BatchRestaurant
        from domain.services.pricing_service import PricingService

        strategies = list(BatchingStrategy.objects.filter(is_active=True))

        if not strategies:
            logger.warning("No active batching strategies found")
            return None

        from apps.core.geo_utils import get_zone_ids_from_h3, get_neighboring_zone_ids, get_zone_h3_cells
        
        zone_info = get_neighboring_zone_ids(order.h3_index, max_distance_km=30)
        primary_zone_ids = zone_info.get('same_zone', [])
        neighbor_zone_ids = zone_info.get('neighbor_zones', [])
        
        if not primary_zone_ids:
            logger.warning(f"Order {order.id} is not in any active operational zone")
            return None

        neighbor_cells = list(h3.grid_disk(order.h3_index, max(s.neighbor_ring_k for s in strategies)))

        from apps.geo.models import ZoneH3Index
        
        primary_h3_cells = get_zone_h3_cells(primary_zone_ids)
        valid_h3_cells = set(neighbor_cells) & primary_h3_cells
        
        candidate_orders = []
        is_inter_zone = False
        
        if valid_h3_cells:
            candidate_orders = list(Order.objects.filter(
                status='READY',
                h3_index__in=valid_h3_cells,
                batch__isnull=True
            ).select_related('restaurant', 'customer')[:50])

        if len(candidate_orders) < 2 and neighbor_zone_ids:
            neighbor_h3_cells = get_zone_h3_cells(neighbor_zone_ids)
            expanded_cells = (set(neighbor_cells) & neighbor_h3_cells) - valid_h3_cells
            
            if expanded_cells:
                inter_zone_orders = list(Order.objects.filter(
                    status='READY',
                    h3_index__in=expanded_cells,
                    batch__isnull=True
                ).select_related('restaurant', 'customer')[:20])
                
                if inter_zone_orders:
                    candidate_orders.extend(inter_zone_orders)
                    is_inter_zone = True

        if len(candidate_orders) < 1:
            return None

        if len(candidate_orders) == 1:
            single_order = candidate_orders[0]
            batch = BatchingEngine._create_single_order_batch(single_order)
            if batch:
                batch.is_inter_zone = False
                batch.zone_ids = primary_zone_ids
                batch.save()
            return batch

        all_zone_ids = list(set(primary_zone_ids + neighbor_zone_ids))
        
        def get_order_zones(orders):
            from apps.core.geo_utils import get_zone_ids_from_h3
            zones = set()
            for o in orders:
                if o.h3_index:
                    order_zones = get_zone_ids_from_h3(o.h3_index, radius=0)
                    zones.update(order_zones)
            return list(zones)
        
        for strategy in strategies:
            try:
                cluster = BatchingEngine._find_cluster(candidate_orders, order, strategy)
                if not cluster:
                    continue

                if not BatchingEngine._validate_cluster(cluster, strategy):
                    continue
                
                cluster_zones = get_order_zones(cluster)
                cluster_is_inter_zone = len(cluster_zones) > 1 and is_inter_zone

                batch = BatchingEngine._create_batch(cluster, strategy, cluster_zones, cluster_is_inter_zone)
                if batch:
                    logger.info(f"Created {strategy.name} batch {batch.id} with {len(cluster)} orders")
                    return batch

            except Exception as e:
                logger.error(f"Strategy {strategy.name} failed: {str(e)}")
                continue

        return None

    @staticmethod
    def _find_cluster(candidate_orders, trigger_order, strategy):
        """Find a cluster of orders that fit the strategy"""
        from apps.core.geo_utils import latlng_to_h3

        cluster_by = strategy.cluster_by
        resolution = strategy.h3_resolution

        if cluster_by == 'restaurant':
            if not trigger_order.restaurant_id:
                return None
            target_restaurant_id = trigger_order.restaurant_id
            cluster = [o for o in candidate_orders if o.restaurant_id == target_restaurant_id]
            return cluster[:strategy.get_constraint('max_orders', 5)] if len(cluster) >= strategy.get_constraint('min_orders', 2) else None

        elif cluster_by == 'delivery_h3':
            clusters = {}
            for order in candidate_orders:
                if not order.delivery_location:
                    continue
                delivery_h3 = latlng_to_h3(
                    order.delivery_location.y,
                    order.delivery_location.x,
                    resolution
                )
                if not delivery_h3:
                    continue
                if delivery_h3 not in clusters:
                    clusters[delivery_h3] = []
                clusters[delivery_h3].append(order)

            merged = BatchingEngine._merge_nearby_clusters(clusters, resolution)

            trigger_h3 = None
            if trigger_order.delivery_location:
                trigger_h3 = latlng_to_h3(
                    trigger_order.delivery_location.y,
                    trigger_order.delivery_location.x,
                    resolution
                )

            if trigger_h3 and trigger_h3 in merged:
                return merged[trigger_h3][:strategy.get_constraint('max_orders', 5)]

            for key, orders in merged.items():
                if len(orders) >= strategy.get_constraint('min_orders', 2):
                    return orders[:strategy.get_constraint('max_orders', 5)]

            return None

        elif cluster_by == 'time_window':
            time_window_min = strategy.get_constraint('time_window_min', 15)
            from django.utils import timezone
            cutoff = timezone.now() - timezone.timedelta(minutes=time_window_min)

            time_cluster = [o for o in candidate_orders if o.ready_at and o.ready_at >= cutoff]
            return time_cluster[:strategy.get_constraint('max_orders', 5)] if len(time_cluster) >= strategy.get_constraint('min_orders', 2) else None

        return None

    @staticmethod
    def _merge_nearby_clusters(clusters, resolution):
        """Merge clusters in nearby H3 cells"""
        if not clusters:
            return {}

        merged = {}
        processed = set()

        for cell in list(clusters.keys()):
            if cell in processed:
                continue

            key = f"{cell[:8]}_merged"
            neighbor_cluster = []
            neighbors = h3.grid_disk(cell, 1)

            for neighbor in neighbors:
                if neighbor in clusters:
                    for order in clusters[neighbor]:
                        if id(order) not in [id(o) for o in neighbor_cluster]:
                            neighbor_cluster.append(order)
                    processed.add(neighbor)

            if neighbor_cluster:
                merged[key] = neighbor_cluster

        return merged if merged else clusters

    @staticmethod
    def _validate_cluster(orders, strategy):
        """Validate cluster against strategy constraints"""
        if len(orders) < strategy.get_constraint('min_orders', 2):
            return False

        unique_restaurants = set(o.restaurant_id for o in orders)

        max_restaurants = strategy.get_constraint('max_restaurants')
        if max_restaurants and len(unique_restaurants) > max_restaurants:
            return False

        min_restaurants = strategy.get_constraint('min_restaurants')
        if min_restaurants and len(unique_restaurants) < min_restaurants:
            return False

        max_distance = strategy.get_constraint('max_distance_km')
        if max_distance:
            if not BatchingEngine._check_distance_constraint(orders, max_distance, strategy):
                return False

        return True

    @staticmethod
    def _check_distance_constraint(orders, max_distance_km, strategy):
        """Check if all orders are within max distance"""
        import math

        if strategy.cluster_by == 'restaurant':
            return True

        if not orders or not orders[0].delivery_location:
            return True

        from apps.core.geo_utils import latlng_to_h3
        ref_h3 = latlng_to_h3(
            orders[0].delivery_location.y,
            orders[0].delivery_location.x,
            strategy.h3_resolution
        )

        for order in orders:
            if not order.delivery_location:
                continue
            order_h3 = latlng_to_h3(
                order.delivery_location.y,
                order.delivery_location.x,
                strategy.h3_resolution
            )
            if order_h3:
                r_lat, r_lng = h3.cell_to_latlng(ref_h3)
                o_lat, o_lng = h3.cell_to_latlng(order_h3)
                distance = h3.great_circle_distance((r_lat, r_lng), (o_lat, o_lng))
                if distance > max_distance_km:
                    return False

        return True

    @staticmethod
    @transaction.atomic
    def _create_batch(orders, strategy, zone_ids=None, is_inter_zone=False):
        from apps.orders.models import Order, DeliveryBatch, BatchRestaurant

        order_ids = [str(o.id) for o in orders]
        locked_orders = list(Order.objects.select_for_update(
            skip_locked=True
        ).filter(
            id__in=order_ids,
            batch__isnull=True
        ).select_related('restaurant'))

        if not locked_orders or len(locked_orders) != len(orders):
            logger.info(f"Orders already batched by another worker")
            return None

        route = None
        if strategy.route_optimization.get('enabled'):
            route = BatchingEngine._optimize_route(
                locked_orders,
                strategy.route_optimization
            )

        pricing_result = BatchingEngine._calculate_pricing(locked_orders)

        driver_payout = Decimal('0')
        route_distance = Decimal('0')

        min_guarantee = Decimal('200')
        per_km_rate = Decimal('50')
        per_stop_rate = Decimal('30')

        from domain.services.pricing_service import PricingService
        policy = PricingService.get_active_policy()
        if policy:
            if policy.driver_min_guarantee:
                min_guarantee = policy.driver_min_guarantee
            if policy.driver_per_km_rate:
                per_km_rate = policy.driver_per_km_rate
            if policy.driver_per_stop_rate:
                per_stop_rate = policy.driver_per_stop_rate

        route_dict = None
        route_distance = Decimal('0')

        if route:
            if hasattr(route, 'to_dict'):
                route_dict = route.to_dict()
            elif isinstance(route, dict):
                route_dict = route

        if route_dict and route_dict.get('total_distance_km'):
            route_distance = Decimal(str(route_dict['total_distance_km']))

            stop_sequence = route_dict.get('stop_sequence', [])
            num_pickups = len([s for s in stop_sequence if s.get('type') == 'pickup'])
            num_deliveries = len([s for s in stop_sequence if s.get('type') == 'delivery'])
            total_stops = num_pickups + num_deliveries

            driver_payout = (
                min_guarantee +
                (route_distance * per_km_rate) +
                (Decimal(str(total_stops)) * per_stop_rate)
            )
        else:
            total_delivery_fees = sum(Decimal(str(o.delivery_fee or 0)) for o in locked_orders)
            driver_payout = max(min_guarantee, total_delivery_fees * Decimal('0.90'))

        total_service_fees = sum(Decimal(str(o.service_fee or 0)) for o in locked_orders)
        user_paid_total = pricing_result['original_total'] + total_service_fees

        if driver_payout > user_paid_total:
            logger.warning(f"Driver payout capped for batch: {driver_payout} -> {user_paid_total}")
            driver_payout = user_paid_total

        if driver_payout < Decimal('0'):
            driver_payout = Decimal('0')

        company_profit = user_paid_total - driver_payout

        batch_type = 'INTER_ZONE' if is_inter_zone else strategy.name
        
        batch = DeliveryBatch.objects.create(
            batch_type=batch_type,
            status='CREATED',
            route_optimization=route_dict,
            user_paid_total=user_paid_total,
            driver_payout=driver_payout,
            company_profit=company_profit,
            route_distance_km=route_distance,
            zone_ids=zone_ids,
            is_inter_zone=is_inter_zone,
            offer_attempt=0,
        )

        if route_dict:
            from domain.services.mapbox_routing_service import MapboxRoutingService
            MapboxRoutingService._emit_event('success', batch.id, route_dict)

        from domain.services.batch_dispatch_service import BatchDispatchService

        from django.db import transaction
        transaction.on_commit(lambda: BatchDispatchService.dispatch_batch(str(batch.id)))

        delivery_seq_map = {}
        if route_dict:
            seq = 1
            for stop in route_dict.get('stop_sequence', []):
                if stop.get('type') == 'delivery' and stop.get('order_id'):
                    delivery_seq_map[stop['order_id']] = seq
                    seq += 1

        for order in locked_orders:
            order.batch = batch
            order.route_sequence = delivery_seq_map.get(str(order.id))

        Order.objects.bulk_update(
            locked_orders,
            ['batch', 'route_sequence']
        )

        if strategy.cluster_by in ('delivery_h3', 'hybrid', 'time_window'):
            restaurants_in_batch = {o.restaurant_id for o in locked_orders}
            seq = 1
            for restaurant_id in sorted(restaurants_in_batch):
                BatchRestaurant.objects.create(
                    batch=batch,
                    restaurant_id=restaurant_id,
                    pickup_sequence=seq
                )
                seq += 1

        return batch

    @staticmethod
    @transaction.atomic
    def _create_single_order_batch(order):
        """Create a single-order batch with distance-based driver payout"""
        from apps.orders.models import Order, DeliveryBatch
        from domain.services.pricing_service import PricingService
        from domain.services.mapbox_routing_service import MapboxRoutingService

        locked_order = Order.objects.select_for_update().filter(
            id=order.id,
            batch__isnull=True
        ).select_related('restaurant').first()

        if not locked_order:
            logger.info(f"Order {order.id} already batched by another worker")
            return None

        policy = PricingService.get_active_policy()
        min_guarantee = policy.driver_min_guarantee if policy and policy.driver_min_guarantee else Decimal('200')
        per_km_rate = policy.driver_per_km_rate if policy and policy.driver_per_km_rate else Decimal('50')
        per_stop_rate = policy.driver_per_stop_rate if policy and policy.driver_per_stop_rate else Decimal('30')
        driver_share = policy.single_order_driver_share if policy and policy.single_order_driver_share else Decimal('0.90')

        delivery_fee = Decimal(str(locked_order.delivery_fee or 0))
        service_fee = Decimal(str(locked_order.service_fee or 0))
        user_paid_total = delivery_fee + service_fee

        route_dict = None
        route_distance = Decimal('0')
        route = MapboxRoutingService.get_route_for_batch([locked_order])

        if route:
            if hasattr(route, 'to_dict'):
                route_dict = route.to_dict()
            else:
                route_dict = route

            if route_dict and route_dict.get('total_distance_km'):
                route_distance = Decimal(str(route_dict['total_distance_km']))

        driver_payout = delivery_fee * driver_share

        if route_distance > 0 and per_km_rate > 0 and per_stop_rate > 0:
            calculated_payout = (
                min_guarantee +
                (route_distance * per_km_rate) +
                (Decimal('2') * per_stop_rate)
            )
            driver_payout = max(driver_payout, calculated_payout)

        if driver_payout > user_paid_total:
            driver_payout = user_paid_total

        if driver_payout < Decimal('0'):
            driver_payout = Decimal('0')

        company_profit = user_paid_total - driver_payout

        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            route_optimization=route_dict,
            route_distance_km=route_distance,
            user_paid_total=user_paid_total,
            driver_payout=driver_payout,
            company_profit=company_profit,
            offer_attempt=0,
        )

        locked_order.batch = batch
        locked_order.route_sequence = 1
        locked_order.save()

        if route_dict:
            MapboxRoutingService._emit_event('success', batch.id, route_dict)

        from domain.services.batch_dispatch_service import BatchDispatchService
        from django.db import transaction
        transaction.on_commit(lambda: BatchDispatchService.dispatch_batch(str(batch.id)))
        
        logger.info(f"Created single-order batch {batch.id} for order {locked_order.id}")
        return batch

    @staticmethod
    def _optimize_route(orders, config, batch_id=None):
        """Optimize delivery route using Mapbox Routing API"""
        from domain.services.mapbox_routing_service import MapboxRoutingService

        return MapboxRoutingService.get_route_for_batch(orders, batch_id=batch_id)

    @staticmethod
    def _calculate_pricing(orders):
        """
        Calculate pricing for batch - uses stored fees from orders when available.
        Only recalculates if fees are missing (backwards compatibility).
        Skips audit logs for batch recalculation to avoid duplicate logs.
        """
        original_total = Decimal('0')
        per_order = {}

        for order in orders:
            fee = order.delivery_fee or Decimal('0')
            if fee == Decimal('0') and order.restaurant and order.delivery_location:
                fee, _, _ = PricingService.calculate_delivery_fee(
                    order.restaurant,
                    order.delivery_location,
                    order=order,
                    skip_log=True
                )
            original_total += fee
            per_order[str(order.id)] = {
                'original': fee,
                'final': fee
            }

        return {
            'original_total': original_total,
            'per_order': per_order
        }