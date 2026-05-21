"""
CrossBatchingService — DEPRECATED.

CrossBatchingService.attempt_batching() is no longer called.
All cross-restaurant batching is now handled by BatchPipeline
(domain/services/batch_pipeline.py) in a unified single code path.

CrossBatchDriverMatcher is STILL ACTIVE and used by BatchDispatchService
for driver selection scoring. Do not remove it.
"""
from django.db import transaction
from django.utils import timezone
from django.core.cache import cache
from decimal import Decimal
import logging
import math
import h3

logger = logging.getLogger(__name__)

from domain.services.mapbox_routing_service import MapboxRoutingService

BATCHING_LOCK_TTL = 30

MAX_RESTAURANTS_PER_BATCH = 3
MAX_ORDERS_PER_BATCH = 5
MAX_ORDERS_QUERY_LIMIT = 50
NEIGHBOR_RING_K = 2
BATCH_CLUSTER_RESOLUTION = 10
CROSS_BATCH_DISCOUNT = Decimal('0.15')

DISTANCE_WEIGHT = 0.6
TIME_WEIGHT = 0.4
MAX_EXPECTED_DISTANCE = 20
MAX_EXPECTED_TIME = 60
TRAVEL_TIME_PER_KM = 3
RESTAURANT_PICKUP_TIME = 4
DELIVERY_STOP_TIME = 2


class CrossBatchingService:
    """
    DEPRECATED — replaced by BatchPipeline.

    CrossBatchDriverMatcher below is still active.
    """

    @staticmethod
    def _merge_nearby_clusters(clusters, resolution=10):
        return clusters

    @staticmethod
    def find_best_driver_for_batch(batch, max_radius_km=15):
        return CrossBatchDriverMatcher.find_best_driver_for_batch(batch, max_radius_km)

    @staticmethod
    def attempt_batching(h3_index):
        logger.warning(
            "[CrossBatchingService] DEPRECATED — attempt_batching() called. "
            "Use BatchPipeline.run(order) instead. h3_index=%s", h3_index
        )
        return None



    @staticmethod
    def attempt_batching(h3_index):
        lock_key = f"cross_batch_lock:{h3_index[:8]}"
        lock_acquired = cache.add(lock_key, "1", BATCHING_LOCK_TTL)

        if not lock_acquired:
            logger.debug(f"Another worker is processing batching for H3 {h3_index[:8]}, skipping")
            return None

        try:
            return CrossBatchingService._process_batching(h3_index)
        finally:
            cache.delete(lock_key)

    @staticmethod
    @transaction.atomic
    def _process_batching(h3_index):
        from apps.orders.models import Order, DeliveryBatch, BatchRestaurant
        from domain.services.pricing_service import PricingService
        from apps.core.geo_utils import get_zone_ids_from_h3

        zone_ids = get_zone_ids_from_h3(h3_index, radius=1)
        if not zone_ids:
            logger.warning(f"H3 index {h3_index} is not in any active operational zone")
            return None

        from apps.geo.models import ZoneH3Index
        zone_h3_cells = ZoneH3Index.objects.filter(
            zone_id__in=zone_ids
        ).values_list('h3_index', flat=True).distinct()
        
        neighbor_cells = list(h3.grid_disk(h3_index, NEIGHBOR_RING_K))
        valid_h3_cells = set(neighbor_cells) & set(zone_h3_cells)
        
        if not valid_h3_cells:
            logger.warning(f"No valid H3 cells in zones for H3 index {h3_index}")
            return None

        orders = list(Order.objects.select_for_update().filter(
            status='READY',
            h3_index__in=valid_h3_cells,
            batch__isnull=True
        ).select_related('restaurant', 'customer')[:MAX_ORDERS_QUERY_LIMIT])

        if len(orders) < 2:
            return None

        clusters = CrossBatchingService._cluster_orders_by_delivery_h3(orders)

        created_batches = []
        for cluster_key, cluster_orders in clusters.items():
            if len(cluster_orders) < 2:
                continue

            unique_restaurants = set(o.restaurant_id for o in cluster_orders)
            if len(unique_restaurants) < 2:
                continue

            if len(cluster_orders) > MAX_ORDERS_PER_BATCH:
                cluster_orders = cluster_orders[:MAX_ORDERS_PER_BATCH]
                unique_restaurants = set(o.restaurant_id for o in cluster_orders)

            if len(unique_restaurants) > MAX_RESTAURANTS_PER_BATCH:
                continue

            if len(unique_restaurants) < 2:
                logger.info(f"Cluster reduced to single restaurant, skipping cross-batch")
                continue

            order_ids = [str(o.id) for o in cluster_orders]
            locked_order_ids = CrossBatchingService._lock_orders_atomically(order_ids)
            if not locked_order_ids or len(locked_order_ids) != len(order_ids):
                logger.info(f"Orders already assigned by another worker, skipping batch creation")
                continue

            orders_to_batch = list(Order.objects.filter(
                id__in=locked_order_ids
            ).select_related('restaurant').order_by('id'))

            if len(orders_to_batch) != len(locked_order_ids):
                logger.warning(f"Order count mismatch after lock, skipping batch")
                continue

            route_result = CrossBatchingService._optimize_route(orders_to_batch)

            min_guarantee = Decimal('200')
            per_km_rate = Decimal('50')
            per_stop_rate = Decimal('30')

            policy = PricingService.get_active_policy()
            if policy:
                if policy.driver_min_guarantee:
                    min_guarantee = policy.driver_min_guarantee
                if policy.driver_per_km_rate:
                    per_km_rate = policy.driver_per_km_rate
                if policy.driver_per_stop_rate:
                    per_stop_rate = policy.driver_per_stop_rate

            route_distance = Decimal(str(route_result.get('total_distance_km', 0)))
            num_pickups = len([s for s in route_result['sequence'] if s.get('type') == 'pickup'])
            num_deliveries = len([s for s in route_result['sequence'] if s.get('type') == 'delivery'])
            total_stops = num_pickups + num_deliveries

            driver_payout = (
                min_guarantee +
                (route_distance * per_km_rate) +
                (Decimal(str(total_stops)) * per_stop_rate)
            )

            batch = DeliveryBatch.objects.create(
                batch_type='CROSS_RESTAURANT',
                status='CREATED',
                route_optimization=route_result['sequence'],
                route_distance_km=route_distance,
                discount_rate=CROSS_BATCH_DISCOUNT,
                driver_payout=driver_payout,
                offer_attempt=0,
                zone_ids=zone_ids,
                is_inter_zone=False,
            )

            delivery_order_map = {}
            delivery_seq = 1
            for stop in route_result['sequence']:
                if stop['type'] == 'delivery' and stop.get('order_id'):
                    delivery_order_map[stop['order_id']] = delivery_seq
                    delivery_seq += 1

            fee_result = PricingService.calculate_batch_fee(orders_to_batch, 'CROSS_RESTAURANT')

            total_delivery_fees = fee_result['original_total']
            total_service_fees = sum(Decimal(str(o.service_fee or 0)) for o in orders_to_batch)
            user_paid_total = total_delivery_fees + total_service_fees

            if driver_payout > user_paid_total:
                driver_payout = user_paid_total
                logger.warning(f"Driver payout capped for batch. Original > user_paid_total")

            company_profit = user_paid_total - driver_payout

            batch.driver_payout = driver_payout
            batch.company_profit = company_profit
            batch.original_total_fee = fee_result['original_total']
            batch.discounted_total_fee = fee_result['discounted_total']
            batch.save()

            fee_map = {fee_info['order_id']: fee_info for fee_info in fee_result['per_order_fees']}

            for order in orders_to_batch:
                fee_info = fee_map.get(str(order.id))
                order_id_str = str(order.id)
                if fee_info:
                    order.batch = batch
                    order.original_delivery_fee = fee_info['original_fee']
                    order.delivery_fee = fee_info['discounted_fee']
                    order.discount_applied = True
                if order_id_str in delivery_order_map:
                    order.route_sequence = delivery_order_map[order_id_str]

            Order.objects.bulk_update(
                orders_to_batch,
                ['batch', 'original_delivery_fee', 'delivery_fee', 'discount_applied', 'route_sequence']
            )

            restaurant_seq = 1
            for stop in route_result['sequence']:
                if stop['type'] == 'pickup':
                    BatchRestaurant.objects.create(
                        batch=batch,
                        restaurant_id=stop['restaurant_id'],
                        pickup_sequence=restaurant_seq
                    )
                    restaurant_seq += 1

            logger.info(f"Created cross-restaurant batch {batch.id} with {len(orders_to_batch)} orders, {len(unique_restaurants)} restaurants")
            created_batches.append(batch)

        return created_batches if created_batches else None

    @staticmethod
    def _lock_orders_atomically(order_ids):
        from apps.orders.models import Order
        from django.db import transaction

        if not order_ids:
            return []

        with transaction.atomic():
            locked_orders = Order.objects.select_for_update(
                skip_locked=True
            ).filter(
                id__in=order_ids,
                batch__isnull=True
            ).values_list('id', flat=True)

            return list(locked_orders)

    @staticmethod
    def _cluster_orders_by_delivery_h3(orders, resolution=BATCH_CLUSTER_RESOLUTION):
        from apps.core.geo_utils import latlng_to_h3

        clusters = {}
        for order in orders:
            if not order.delivery_location:
                logger.warning(f"Order {order.id} has no delivery_location, skipping from batching")
                continue

            delivery_h3 = latlng_to_h3(
                order.delivery_location.y,
                order.delivery_location.x,
                resolution
            )

            if not delivery_h3:
                logger.warning(f"Order {order.id} failed H3 conversion, skipping from batching")
                continue

            if delivery_h3 not in clusters:
                clusters[delivery_h3] = []
            clusters[delivery_h3].append(order)

        neighbor_clusters = {}
        seen_orders = set()

        for cell in list(clusters.keys()):
            neighbors = h3.grid_disk(cell, 1)
            key = f"{cell[:8]}_cluster"
            for neighbor in neighbors:
                if neighbor in clusters:
                    if key not in neighbor_clusters:
                        neighbor_clusters[key] = []
                    for order in clusters[neighbor]:
                        if order.id not in seen_orders:
                            neighbor_clusters[key].append(order)
                            seen_orders.add(order.id)

        if not neighbor_clusters:
            return clusters

        return neighbor_clusters

    @staticmethod
    def _optimize_route(orders):
        from domain.services.mapbox_routing_service import MapboxRoutingService

        route = MapboxRoutingService.get_route_for_batch(orders)

        if route:
            route_dict = route.to_dict()
            return {
                'sequence': route_dict.get('stop_sequence', []),
                'total_distance_km': route_dict.get('total_distance_km', 0),
                'estimated_time_min': route_dict.get('total_duration_min', 0),
                'score': 1.0,
                'distance_weight_contribution': 0.5,
                'time_weight_contribution': 0.5,
                'route_object': route
            }

        logger.warning("Mapbox routing failed, falling back to custom optimizer")
        restaurant_locs = {}
        for order in orders:
            rid = order.restaurant_id
            if rid not in restaurant_locs:
                restaurant_locs[rid] = {
                    'restaurant_id': rid,
                    'lat': order.restaurant.geo_location.y,
                    'lng': order.restaurant.geo_location.x,
                    'name': order.restaurant.name
                }

        delivery_locs = []
        for order in orders:
            delivery_locs.append({
                'order_id': str(order.id),
                'order_uuid': order.id,
                'lat': order.delivery_location.y,
                'lng': order.delivery_location.x,
                'address': order.delivery_address
            })

        pickups = list(restaurant_locs.values())
        all_locations = pickups + delivery_locs

        initial_route = CrossBatchingService._nearest_neighbor_route(pickups, delivery_locs)
        optimized_route = CrossBatchingService._two_opt_improvement(initial_route, all_locations)

        total_distance = CrossBatchingService._calculate_distance(optimized_route, restaurant_locs, {str(o.id): o for o in orders})
        total_time = CrossBatchingService._estimate_time(total_distance, len(pickups), len(delivery_locs))

        norm_distance = min(total_distance / MAX_EXPECTED_DISTANCE, 1.0)
        norm_time = min(total_time / MAX_EXPECTED_TIME, 1.0)
        score = (DISTANCE_WEIGHT * norm_distance) + (TIME_WEIGHT * norm_time)

        return {
            'sequence': optimized_route,
            'total_distance_km': round(total_distance, 2),
            'estimated_time_min': total_time,
            'score': round(score, 3),
            'distance_weight_contribution': round(DISTANCE_WEIGHT * norm_distance, 3),
            'time_weight_contribution': round(TIME_WEIGHT * norm_time, 3)
        }

    @staticmethod
    def _nearest_neighbor_route(pickups, deliveries):
        if not pickups or not deliveries:
            return []

        route = []
        current = {'lat': pickups[0]['lat'], 'lng': pickups[0]['lng']}
        remaining_pickups = pickups.copy()
        remaining_deliveries = deliveries.copy()

        while remaining_pickups or remaining_deliveries:
            if remaining_pickups:
                nearest_pickup = min(remaining_pickups, key=lambda p: CrossBatchingService._haversine(
                    current['lat'], current['lng'], p['lat'], p['lng']
                ))
                route.append({
                    'type': 'pickup',
                    'restaurant_id': nearest_pickup['restaurant_id'],
                    'restaurant_name': nearest_pickup['name']
                })
                current = {'lat': nearest_pickup['lat'], 'lng': nearest_pickup['lng']}
                remaining_pickups.remove(nearest_pickup)
            elif remaining_deliveries:
                nearest_delivery = min(remaining_deliveries, key=lambda d: CrossBatchingService._haversine(
                    current['lat'], current['lng'], d['lat'], d['lng']
                ))
                route.append({
                    'type': 'delivery',
                    'order_id': nearest_delivery['order_id'],
                    'address': nearest_delivery['address']
                })
                current = {'lat': nearest_delivery['lat'], 'lng': nearest_delivery['lng']}
                remaining_deliveries.remove(nearest_delivery)

        return route

    @staticmethod
    def _two_opt_improvement(route, all_locations):
        if len(route) < 4:
            return route

        improved = True
        best_route = route.copy()

        while improved:
            improved = False
            for i in range(len(best_route) - 2):
                for j in range(i + 2, len(best_route)):
                    new_route = best_route[:i+1] + best_route[i+1:j+1][::-1] + best_route[j+1:]
                    if CrossBatchingService._route_distance(new_route, all_locations) < \
                       CrossBatchingService._route_distance(best_route, all_locations):
                        best_route = new_route
                        improved = True

        return best_route

    @staticmethod
    def _route_distance(route, all_locations):
        total = 0
        loc_map = {l.get('restaurant_id'): l for l in all_locations if l.get('restaurant_id')}
        loc_map.update({l.get('order_id'): l for l in all_locations if l.get('order_id')})

        for i in range(len(route) - 1):
            loc1 = loc_map.get(route[i].get('restaurant_id') or route[i].get('order_id'))
            loc2 = loc_map.get(route[i+1].get('restaurant_id') or route[i+1].get('order_id'))
            if loc1 and loc2:
                total += CrossBatchingService._haversine(loc1['lat'], loc1['lng'], loc2['lat'], loc2['lng'])
        return total

    @staticmethod
    def _calculate_distance(route, restaurants_dict, orders_dict):
        if len(route) < 2:
            return 0

        def get_lat_lng(loc):
            if not loc:
                return None, None
            if isinstance(loc, dict):
                return loc.get('lat'), loc.get('lng')
            if hasattr(loc, 'delivery_location') and loc.delivery_location:
                return loc.delivery_location.y, loc.delivery_location.x
            if hasattr(loc, 'geo_location') and loc.geo_location:
                return loc.geo_location.y, loc.geo_location.x
            return None, None

        total = 0

        for i in range(len(route) - 1):
            stop = route[i]
            next_stop = route[i + 1]

            if stop['type'] == 'pickup':
                loc1 = restaurants_dict.get(stop['restaurant_id'])
            else:
                loc1 = orders_dict.get(stop['order_id'])

            if next_stop['type'] == 'pickup':
                loc2 = restaurants_dict.get(next_stop['restaurant_id'])
            else:
                loc2 = orders_dict.get(next_stop['order_id'])

            lat1, lng1 = get_lat_lng(loc1)
            lat2, lng2 = get_lat_lng(loc2)

            if lat1 is not None and lng1 is not None and lat2 is not None and lng2 is not None:
                total += CrossBatchingService._haversine(lat1, lng1, lat2, lng2)

        return total

    @staticmethod
    def _estimate_time(total_distance_km, num_pickups, num_deliveries):
        travel_time = total_distance_km * TRAVEL_TIME_PER_KM
        pickup_time = num_pickups * RESTAURANT_PICKUP_TIME
        delivery_time = num_deliveries * DELIVERY_STOP_TIME
        return int(travel_time + pickup_time + delivery_time)

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2):
        R = 6371
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c


class CrossBatchDriverMatcher:

    @staticmethod
    def find_best_driver_for_batch(batch, max_radius_km=15):
        from apps.delivery.models import Driver
        from django.contrib.gis.geos import Point
        from django.contrib.gis.db.models.functions import Distance
        from django.contrib.gis.measure import D
        from django.db.models import Count, Q, F

        route = batch.route_optimization
        stop_sequence = route.get('stop_sequence', []) if isinstance(route, dict) else route

        if not stop_sequence or len(stop_sequence) == 0:
            logger.warning(f"Batch {batch.id} has no route optimization")
            return None

        first_stop = stop_sequence[0]
        if first_stop.get('type') != 'pickup':
            logger.warning(f"Batch {batch.id} route has invalid first stop type: {first_stop.get('type')}")
            return None

        first_restaurant_id = first_stop.get('restaurant_id')
        if not first_restaurant_id:
            first_order = batch.orders.first()
            if first_order and first_order.restaurant_id:
                first_restaurant_id = str(first_order.restaurant_id)

        if not first_restaurant_id:
            logger.warning(f"Batch {batch.id} route has invalid first stop (missing restaurant_id)")
            return None

        try:
            from apps.restaurants.models import Restaurant
            first_restaurant = Restaurant.objects.get(id=first_restaurant_id)
            restaurant_point = Point(
                float(first_restaurant.geo_location.x),
                float(first_restaurant.geo_location.y),
                srid=4326
            )
        except Restaurant.DoesNotExist:
            logger.error(f"Restaurant {first_restaurant_id} not found for batch {batch.id}")
            return None

        batch_order_count = batch.orders.count()
        
        first_order = batch.orders.first()
        if not first_order or not first_order.h3_index:
            logger.warning(f"Batch {batch.id} has no orders with H3 index")
            return None
        
        from apps.core.geo_utils import get_zone_ids_from_h3, get_neighboring_zone_ids, get_zone_h3_cells
        
        batch_zone_ids = batch.zone_ids if hasattr(batch, 'zone_ids') and batch.zone_ids else []
        if not batch_zone_ids and first_order.h3_index:
            batch_zone_ids = get_zone_ids_from_h3(first_order.h3_index, radius=1)
        
        if not batch_zone_ids:
            logger.warning(f"Batch {batch.id} orders are not in any active zone")
            return None

        from apps.geo.models import ZoneH3Index
        
        zone_info = get_neighboring_zone_ids(first_order.h3_index, max_distance_km=30)
        primary_zone_ids = zone_info.get('same_zone', [])
        neighbor_zone_ids = zone_info.get('neighbor_zones', [])
        
        primary_h3_cells = get_zone_h3_cells(primary_zone_ids)
        
        if not primary_h3_cells:
            logger.warning(f"Batch {batch.id} - no primary zone H3 cells found")
            return None

        with transaction.atomic():
            def find_best_driver(zone_cells, zone_penalty=0, filter_q=Q()):
                drivers = Driver.objects.select_for_update(skip_locked=True).filter(
                    status='AVAILABLE',
                    current_active_orders__lt=F('max_active_orders'),
                    h3_index__in=zone_cells,
                    location__dwithin=(restaurant_point, D(km=max_radius_km))
                ).filter(filter_q).annotate(
                    distance_meters=Distance('location', restaurant_point)
                )

                if not drivers.exists():
                    return None, None

                best = None
                min_score = float('inf')

                for driver in drivers:
                    rating_penalty = 5.0 - float(driver.rating)
                    dist_km = driver.distance_meters.km if driver.distance_meters else max_radius_km

                    score = (
                        float(dist_km) * 0.4 +
                        rating_penalty * 0.4 +
                        zone_penalty * 0.2
                    )

                    if score < min_score:
                        min_score = score
                        best = driver

                return best, min_score

            permanent_filter = Q(driver_type='permanent')
            same_zone_driver, same_zone_score = find_best_driver(primary_h3_cells, zone_penalty=0, filter_q=permanent_filter)

            if same_zone_driver:
                logger.info(f"Found permanent driver for batch {batch.id}: score={same_zone_score:.2f}")
                return same_zone_driver

            if neighbor_zone_ids:
                neighbor_h3_cells = get_zone_h3_cells(neighbor_zone_ids)
                if neighbor_h3_cells:
                    neighbor_driver, neighbor_score = find_best_driver(neighbor_h3_cells, zone_penalty=2.0, filter_q=permanent_filter)
                    if neighbor_driver:
                        logger.info(f"Found permanent driver in neighbor zone for batch {batch.id}: score={neighbor_score:.2f}")
                        return neighbor_driver

            all_restaurants = list(batch.orders.values_list('restaurant_id', flat=True).distinct())

            logger.info(f"No permanent drivers found for batch {batch.id}, trying temporary drivers from {len(all_restaurants)} restaurants")
            for restaurant_id in all_restaurants:
                temp_filter = Q(driver_type='temporary', assigned_by_id=restaurant_id)
                temp_driver, temp_score = find_best_driver(primary_h3_cells, zone_penalty=1.0, filter_q=temp_filter)
                if temp_driver:
                    logger.info(f"Found temporary driver for batch {batch.id} from restaurant {restaurant_id}: score={temp_score:.2f}")
                    return temp_driver

                if neighbor_zone_ids:
                    neighbor_h3_cells = get_zone_h3_cells(neighbor_zone_ids)
                    if neighbor_h3_cells:
                        temp_driver, temp_score = find_best_driver(neighbor_h3_cells, zone_penalty=3.0, filter_q=temp_filter)
                        if temp_driver:
                            logger.info(f"Found temporary driver in neighbor zone for batch {batch.id} from restaurant {restaurant_id}: score={temp_score:.2f}")
                            return temp_driver

            logger.warning(f"No drivers available for batch {batch.id} in any zone (permanent or temporary)")
            return None