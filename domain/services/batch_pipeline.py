"""
BatchPipeline — Single, unified batching entry point.

Replaces: BatchingEngine, CrossBatchingService, BatchingService.

Pipeline steps:
  1. Zone check  — order must be inside an active operational zone
  2. Zone lock   — Redis lock scoped to the zone (not an H3 prefix)
  3. Candidates  — fetch READY, un-batched orders in the zone via DB row lock
  4. Cluster     — group by delivery proximity and restaurant constraints
  5. Validate    — min/max orders, distance, restaurant count
  6. Route       — Mapbox first, bounded 2-opt fallback (outside transaction)
  7. Persist     — atomic: create batch + link orders + batch_restaurants
  8. Pricing     — compute fees and driver payout
  9. Dispatch    — on_commit: dispatch_batch_task fires
"""
from __future__ import annotations

import logging
import math
from decimal import Decimal

import h3
from django.core.cache import cache
from django.db import transaction

from apps.core.geo_utils import (
    get_zone_from_h3,
    get_zone_h3_cells,
    latlng_to_h3,
)

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
ZONE_LOCK_TTL = 30          # seconds — how long to hold the zone-level Redis lock
MAX_CANDIDATES = 50         # max orders to fetch per batching cycle
MAX_ORDERS_PER_BATCH = 5
MAX_RESTAURANTS_PER_BATCH = 3
MIN_ORDERS_FOR_BATCH = 2
NEIGHBOR_RING_K = 3         # H3 rings to expand from delivery cell for clustering
CLUSTER_RESOLUTION = 9     # fine resolution for delivery-proximity clustering
MAX_2OPT_ITERATIONS = 20    # hard cap — prevents unbounded loops inside transactions
CROSS_BATCH_DISCOUNT = Decimal('0.15')

# Fallback route estimation constants
TRAVEL_TIME_PER_KM = 3      # minutes per km
RESTAURANT_PICKUP_TIME = 4  # minutes per pickup stop
DELIVERY_STOP_TIME = 2      # minutes per delivery stop


# ─── Public API ───────────────────────────────────────────────────────────────

class BatchPipeline:
    """
    Single entry point for all batching logic.
    Call BatchPipeline.run(order) when an order becomes READY.
    """

    @staticmethod
    def run(order):
        """
        Attempt to batch a READY order.

        Returns the created DeliveryBatch, or None if no batch was formed.
        This call is safe to invoke from inside a transaction — the actual
        batch dispatch is deferred via on_commit.
        """
        if not order.h3_index:
            logger.debug(f"[BatchPipeline] Order {order.id} has no h3_index — skipping")
            return None

        zone = get_zone_from_h3(order.h3_index)
        if not zone:
            logger.warning(
                f"[BatchPipeline] Order {order.id} h3_index={order.h3_index} "
                f"is not inside any active operational zone — skipping batching"
            )
            return None

        # Zone-scoped lock — one worker per zone at a time
        lock_key = f"batch_zone_lock:{zone.id}"
        acquired = cache.add(lock_key, "1", ZONE_LOCK_TTL)
        if not acquired:
            logger.debug(
                f"[BatchPipeline] Zone {zone.id} locked by another worker — "
                f"order {order.id} will be picked up in next cycle"
            )
            return None

        try:
            return BatchPipeline._run_pipeline(order, zone)
        except Exception as exc:
            logger.error(
                f"[BatchPipeline] Unhandled error for order {order.id}: {exc}",
                exc_info=True,
            )
            return None
        finally:
            cache.delete(lock_key)


    # ─── Internal pipeline ────────────────────────────────────────────────────

    @staticmethod
    def _run_pipeline(order, zone):
        """Execute the full pipeline for one zone-locked order."""
        zone_h3_cells = get_zone_h3_cells([zone.id])
        if not zone_h3_cells:
            logger.warning(f"[BatchPipeline] Zone {zone.id} has no H3 cells seeded")
            return None

        # Expand search to neighbor cells within the zone boundary
        neighbor_cells = set(h3.grid_disk(order.h3_index, NEIGHBOR_RING_K))
        valid_cells = neighbor_cells & zone_h3_cells

        if not valid_cells:
            valid_cells = {order.h3_index}

        candidates = BatchPipeline._fetch_candidates(valid_cells)

        if not candidates:
            # No other orders — create single-order batch immediately
            return BatchPipeline._create_single_order_batch_atomic(order)

        # Try to form a multi-order cluster
        cluster = BatchPipeline._find_best_cluster(order, candidates)

        if cluster and len(cluster) >= MIN_ORDERS_FOR_BATCH:
            # Route optimization happens OUTSIDE the DB transaction
            route_dict = BatchPipeline._optimize_route(cluster)
            return BatchPipeline._create_multi_order_batch_atomic(
                cluster, route_dict, zone_id=zone.id
            )

        # No suitable cluster — single-order batch
        return BatchPipeline._create_single_order_batch_atomic(order)


    # ─── Candidate fetching ───────────────────────────────────────────────────

    @staticmethod
    def _fetch_candidates(valid_cells: set) -> list:
        """
        Fetch READY, un-batched orders in the valid H3 cells.
        NOTE: This is an optimistic read — no row locks are held here.
        The real concurrency guard is select_for_update(skip_locked=True)
        inside _create_multi_order_batch_atomic. Candidates here may
        include orders grabbed by another worker; they will simply be
        skipped when the atomic method tries to lock them.
        """
        from apps.orders.models import Order

        return list(
            Order.objects.filter(
                status='READY',
                h3_index__in=valid_cells,
                batch__isnull=True,
            )
            .select_related('restaurant', 'customer')
            .order_by('ready_at')[:MAX_CANDIDATES]
        )


    # ─── Clustering ───────────────────────────────────────────────────────────

    @staticmethod
    def _find_best_cluster(trigger_order, candidates: list) -> list | None:
        """
        Find the best cluster of orders that can be delivered together.
        Tries same-restaurant first (cheaper), then cross-restaurant.
        """
        # Strategy 1: Same restaurant (no discount needed, simpler routing)
        same_restaurant = [
            o for o in candidates
            if o.restaurant_id == trigger_order.restaurant_id
        ]
        if len(same_restaurant) >= MIN_ORDERS_FOR_BATCH:
            return _dedup_by_pk(same_restaurant)[:MAX_ORDERS_PER_BATCH]

        # Strategy 2: Cross-restaurant — cluster by delivery proximity
        cross_cluster = BatchPipeline._cluster_by_delivery_proximity(trigger_order, candidates)
        if cross_cluster and len(cross_cluster) >= MIN_ORDERS_FOR_BATCH:
            return cross_cluster

        return None

    @staticmethod
    def _cluster_by_delivery_proximity(trigger_order, orders: list) -> list | None:
        """
        Group orders by delivery H3 cell proximity at fine resolution,
        anchored on the triggering order.
        """
        if not trigger_order.delivery_location:
            return None
        trigger_cell = latlng_to_h3(
            trigger_order.delivery_location.y,
            trigger_order.delivery_location.x,
            CLUSTER_RESOLUTION,
        )
        if not trigger_cell:
            return None

        neighbors = set(h3.grid_disk(trigger_cell, 1))

        cluster_orders = []
        cluster_seen = set()

        # Ensure trigger_order is always included and listed first to avoid pruning
        cluster_orders.append(trigger_order)
        cluster_seen.add(trigger_order.id)

        for order in orders:
            if order.id in cluster_seen:
                continue
            if not order.delivery_location:
                continue
            cell = latlng_to_h3(
                order.delivery_location.y,
                order.delivery_location.x,
                CLUSTER_RESOLUTION,
            )
            if cell in neighbors:
                cluster_orders.append(order)
                cluster_seen.add(order.id)

        # Apply restaurant and order caps
        limited = _cap_by_restaurants(cluster_orders, MAX_ORDERS_PER_BATCH, MAX_RESTAURANTS_PER_BATCH)
        return _dedup_by_pk(limited)


    # ─── Route optimization ───────────────────────────────────────────────────

    @staticmethod
    def _optimize_route(orders: list) -> dict | None:
        """
        Compute the optimal delivery route.
        Tries Mapbox first, falls back to bounded nearest-neighbour + 2-opt.
        This is intentionally called OUTSIDE any database transaction.
        """
        from domain.services.mapbox_routing_service import MapboxRoutingService

        try:
            route = MapboxRoutingService.get_route_for_batch(orders)
            if route:
                return route.to_dict() if hasattr(route, 'to_dict') else route
        except Exception as exc:
            logger.warning(f"[BatchPipeline] Mapbox routing failed: {exc} — using fallback")

        return BatchPipeline._fallback_route(orders)

    @staticmethod
    def _fallback_route(orders: list) -> dict:
        """Bounded nearest-neighbour + 2-opt with hard iteration cap."""
        restaurant_locs: dict = {}
        for o in orders:
            rid = o.restaurant_id
            if rid not in restaurant_locs and o.restaurant.geo_location:
                restaurant_locs[rid] = {
                    'type': 'pickup',
                    'restaurant_id': str(rid),
                    'restaurant_name': o.restaurant.name,
                    'lat': o.restaurant.geo_location.y,
                    'lng': o.restaurant.geo_location.x,
                }

        delivery_locs = [
            {
                'type': 'delivery',
                'order_id': str(o.id),
                'address': o.delivery_address or '',
                'lat': o.delivery_location.y,
                'lng': o.delivery_location.x,
            }
            for o in orders
            if o.delivery_location
        ]

        pickups = list(restaurant_locs.values())
        
        # Optimize pickups and deliveries separately to guarantee all pickups happen before all deliveries
        pickups_route = _nearest_neighbour_single(pickups)
        pickups_route = _two_opt(pickups_route, pickups)
        
        deliveries_route = _nearest_neighbour_single(delivery_locs)
        deliveries_route = _two_opt(deliveries_route, delivery_locs)
        
        route = pickups_route + deliveries_route

        total_km = _route_distance_km(route, pickups + delivery_locs)
        num_pickups = sum(1 for s in route if s['type'] == 'pickup')
        num_deliveries = sum(1 for s in route if s['type'] == 'delivery')
        est_time = int(
            total_km * TRAVEL_TIME_PER_KM
            + num_pickups * RESTAURANT_PICKUP_TIME
            + num_deliveries * DELIVERY_STOP_TIME
        )

        return {
            'stop_sequence': route,
            'total_distance_km': round(total_km, 2),
            'total_duration_min': est_time,
        }


    # ─── Batch persistence ────────────────────────────────────────────────────

    @staticmethod
    @transaction.atomic
    def _create_multi_order_batch_atomic(
        orders: list, route_dict: dict | None, zone_id
    ):
        """
        Atomically create a multi-order batch.
        Uses select_for_update(skip_locked=True) to prevent double-batching.
        """
        from apps.orders.models import Order, DeliveryBatch, BatchRestaurant
        from domain.services.pricing_service import PricingService

        order_ids = [str(o.id) for o in orders]

        # Acquire DB-level row locks — skip any orders already grabbed
        locked_orders = list(
            Order.objects.select_for_update(skip_locked=True)
            .filter(id__in=order_ids, batch__isnull=True)
            .select_related('restaurant')
        )

        if len(locked_orders) < MIN_ORDERS_FOR_BATCH:
            logger.info("[BatchPipeline] Not enough orders after locking — skipping batch")
            return None

        # Determine batch type
        unique_restaurants = {o.restaurant_id for o in locked_orders}
        is_cross = len(unique_restaurants) > 1
        batch_type = 'CROSS_RESTAURANT' if is_cross else 'SINGLE_RESTAURANT'
        discount_rate = CROSS_BATCH_DISCOUNT if is_cross else Decimal('0')

        # Pricing
        pricing = BatchPipeline._calculate_pricing(locked_orders, is_cross)
        driver_payout, user_paid_total = BatchPipeline._calculate_driver_payout(
            locked_orders, route_dict, pricing
        )

        company_profit = max(Decimal('0'), user_paid_total - driver_payout)

        # Route metadata
        route_distance = Decimal('0')
        if route_dict and route_dict.get('total_distance_km'):
            route_distance = Decimal(str(route_dict['total_distance_km']))

        batch = DeliveryBatch.objects.create(
            batch_type=batch_type,
            status='CREATED',
            route_optimization=route_dict,
            route_distance_km=route_distance,
            discount_rate=discount_rate,
            user_paid_total=user_paid_total,
            original_total_fee=pricing['original_total'],
            discounted_total_fee=pricing['discounted_total'],
            driver_payout=driver_payout,
            company_profit=company_profit,
            zone_ids=[str(zone_id)],
            is_inter_zone=False,
            offer_attempt=0,
        )

        # Build delivery sequence map from route
        delivery_seq_map = _build_delivery_seq_map(route_dict)

        # Link orders to batch
        fee_map = pricing['per_order']
        for order in locked_orders:
            order.batch = batch
            order.route_sequence = delivery_seq_map.get(str(order.id))
            if is_cross and str(order.id) in fee_map:
                order.original_delivery_fee = fee_map[str(order.id)]['original']
                order.delivery_fee = fee_map[str(order.id)]['discounted']
                order.discount_applied = True

        fields = ['batch', 'route_sequence']
        if is_cross:
            fields += ['original_delivery_fee', 'delivery_fee', 'discount_applied']
        Order.objects.bulk_update(locked_orders, fields)

        # Pickup sequence — spatially sorted by nearest-first from first pickup
        BatchPipeline._create_batch_restaurants(batch, locked_orders, route_dict)

        # Fire dispatch after transaction commits
        batch_id_str = str(batch.id)
        transaction.on_commit(
            lambda: _schedule_dispatch(batch_id_str)
        )

        logger.info(
            f"[BatchPipeline] Created {batch_type} batch {batch.id} "
            f"with {len(locked_orders)} orders, "
            f"{len(unique_restaurants)} restaurant(s)"
        )
        return batch

    @staticmethod
    def _create_single_order_batch_atomic(order):
        """
        Create a batch for a single order.

        Fix #8: Mapbox network call is now OUTSIDE the DB transaction.
        The old version called MapboxRoutingService inside @transaction.atomic,
        holding row locks for the entire duration of the network call (2-5s).

        Steps:
          1. Optimistic existence check (no lock)
          2. Mapbox route call (outside any transaction)
          3. Atomic persist with row lock
        """
        from apps.orders.models import Order
        from domain.services.mapbox_routing_service import MapboxRoutingService

        # Step 1: Optimistic check — skip early if clearly not batchable
        if not Order.objects.filter(
            id=order.id, batch__isnull=True, status='READY'
        ).exists():
            logger.info(f"[BatchPipeline] Order {order.id} not batchable — skipping")
            return None

        # Step 2: Route outside transaction (no locks held during network call)
        route_dict = None
        route_distance = Decimal('0')
        try:
            route = MapboxRoutingService.get_route_for_batch([order])
            if route:
                route_dict = route.to_dict() if hasattr(route, 'to_dict') else route
                if route_dict and route_dict.get('total_distance_km'):
                    route_distance = Decimal(str(route_dict['total_distance_km']))
        except Exception as exc:
            logger.warning(f"[BatchPipeline] Mapbox failed for single order {order.id}: {exc}")

        # Step 3: Persist atomically with a fresh row lock
        return BatchPipeline._persist_single_order_batch(order.id, route_dict, route_distance)

    @staticmethod
    @transaction.atomic
    def _persist_single_order_batch(order_id, route_dict, route_distance):
        """Atomically create and link a single-order batch (short transaction)."""
        from apps.orders.models import Order, DeliveryBatch
        from domain.services.pricing_service import PricingService

        locked_order = (
            Order.objects.select_for_update(skip_locked=True)
            .filter(id=order_id, batch__isnull=True)
            .select_related('restaurant')
            .first()
        )

        if not locked_order:
            logger.info(f"[BatchPipeline] Order {order_id} was batched by another worker")
            return None

        policy = PricingService.get_active_policy()
        min_guarantee = policy.driver_min_guarantee if policy else Decimal('200')
        per_km_rate = policy.driver_per_km_rate if policy else Decimal('50')
        per_stop_rate = policy.driver_per_stop_rate if policy else Decimal('30')
        driver_share = policy.single_order_driver_share if policy else Decimal('0.90')

        delivery_fee = Decimal(str(locked_order.delivery_fee or 0))
        service_fee = Decimal(str(locked_order.service_fee or 0))
        user_paid_total = delivery_fee + service_fee

        driver_payout = delivery_fee * driver_share
        if route_distance > 0:
            calc = min_guarantee + (route_distance * per_km_rate) + (Decimal('2') * per_stop_rate)
            driver_payout = max(driver_payout, calc)

        driver_payout = min(driver_payout, user_paid_total)
        driver_payout = max(driver_payout, Decimal('0'))
        company_profit = user_paid_total - driver_payout

        batch = DeliveryBatch.objects.create(
            batch_type='SINGLE_ORDER',
            status='CREATED',
            route_optimization=route_dict,
            route_distance_km=route_distance,
            user_paid_total=user_paid_total,
            original_total_fee=delivery_fee,
            discounted_total_fee=delivery_fee,
            driver_payout=driver_payout,
            company_profit=company_profit,
            offer_attempt=0,
        )

        locked_order.batch = batch
        locked_order.route_sequence = 1
        locked_order.save(update_fields=['batch', 'route_sequence'])

        batch_id_str = str(batch.id)
        transaction.on_commit(lambda: _schedule_dispatch(batch_id_str))

        logger.info(f"[BatchPipeline] Created SINGLE_ORDER batch {batch.id} for order {locked_order.id}")
        return batch


    # ─── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _calculate_pricing(orders: list, is_cross: bool) -> dict:
        from domain.services.pricing_service import PricingService

        original_total = Decimal('0')
        per_order: dict = {}

        for order in orders:
            fee = Decimal(str(order.delivery_fee or 0))
            if fee == Decimal('0') and order.restaurant and order.delivery_location:
                fee, _, _ = PricingService.calculate_delivery_fee(
                    order.restaurant, order.delivery_location,
                    order=order, skip_log=True
                )
            discounted = fee * (1 - CROSS_BATCH_DISCOUNT) if is_cross else fee
            original_total += fee
            per_order[str(order.id)] = {'original': fee, 'discounted': discounted}

        discounted_total = (
            original_total * (1 - CROSS_BATCH_DISCOUNT) if is_cross else original_total
        )

        return {
            'original_total': original_total,
            'discounted_total': discounted_total,
            'per_order': per_order,
        }

    @staticmethod
    def _calculate_driver_payout(orders, route_dict, pricing) -> tuple[Decimal, Decimal]:
        from domain.services.pricing_service import PricingService

        policy = PricingService.get_active_policy()
        min_guarantee = policy.driver_min_guarantee if policy else Decimal('200')
        per_km_rate = policy.driver_per_km_rate if policy else Decimal('50')
        per_stop_rate = policy.driver_per_stop_rate if policy else Decimal('30')

        total_service_fees = sum(Decimal(str(o.service_fee or 0)) for o in orders)
        user_paid_total = pricing['original_total'] + total_service_fees

        if route_dict and route_dict.get('total_distance_km'):
            route_distance = Decimal(str(route_dict['total_distance_km']))
            stop_sequence = route_dict.get('stop_sequence', [])
            total_stops = len(stop_sequence)
            driver_payout = (
                min_guarantee
                + (route_distance * per_km_rate)
                + (Decimal(str(total_stops)) * per_stop_rate)
            )
        else:
            total_fees = sum(Decimal(str(o.delivery_fee or 0)) for o in orders)
            driver_payout = max(min_guarantee, total_fees * Decimal('0.90'))

        driver_payout = min(driver_payout, user_paid_total)
        driver_payout = max(driver_payout, Decimal('0'))

        if driver_payout > user_paid_total:
            logger.warning(
                f"[BatchPipeline] Driver payout ({driver_payout}) capped to "
                f"user_paid_total ({user_paid_total})"
            )
        return driver_payout, user_paid_total

    @staticmethod
    def _create_batch_restaurants(batch, orders, route_dict):
        """
        Create BatchRestaurant records in spatial order.
        Fix #11: uses bulk_create instead of N individual INSERT calls.
        """
        from apps.orders.models import BatchRestaurant

        to_create = []
        if route_dict:
            seen: set = set()
            seq = 1
            for stop in route_dict.get('stop_sequence', []):
                if stop.get('type') == 'pickup':
                    rid = stop.get('restaurant_id')
                    if rid and rid not in seen:
                        to_create.append(BatchRestaurant(
                            batch=batch,
                            restaurant_id=rid,
                            pickup_sequence=seq,
                        ))
                        seen.add(rid)
                        seq += 1
        else:
            unique = {o.restaurant_id for o in orders}
            for seq, rid in enumerate(unique, 1):
                to_create.append(BatchRestaurant(
                    batch=batch,
                    restaurant_id=rid,
                    pickup_sequence=seq,
                ))

        if to_create:
            BatchRestaurant.objects.bulk_create(to_create, ignore_conflicts=True)


# ─── Module-level helpers (private) ───────────────────────────────────────────

def _dedup_by_pk(orders: list) -> list:
    """Deduplicate orders by primary key — never by Python id()."""
    seen: set = set()
    result = []
    for o in orders:
        if o.id not in seen:
            result.append(o)
            seen.add(o.id)
    return result


def _cap_by_restaurants(orders: list, max_orders: int, max_restaurants: int) -> list:
    """Trim order list to respect max_orders and max_restaurants constraints."""
    result = []
    restaurants_seen: set = set()
    for o in orders:
        if len(result) >= max_orders:
            break
        if (o.restaurant_id not in restaurants_seen
                and len(restaurants_seen) >= max_restaurants):
            continue
        result.append(o)
        restaurants_seen.add(o.restaurant_id)
    return result


def _build_delivery_seq_map(route_dict: dict | None) -> dict:
    """Build {order_id_str: sequence_number} from route stop_sequence."""
    if not route_dict:
        return {}
    seq_map: dict = {}
    seq = 1
    for stop in route_dict.get('stop_sequence', []):
        if stop.get('type') == 'delivery' and stop.get('order_id'):
            seq_map[str(stop['order_id'])] = seq
            seq += 1
    return seq_map


def _schedule_dispatch(batch_id: str):
    """Schedule the Celery dispatch task (called via on_commit)."""
    try:
        from domain.services.dispatch_tasks import dispatch_batch_task
        dispatch_batch_task.apply_async(args=[batch_id], countdown=0)
    except Exception as exc:
        logger.error(f"[BatchPipeline] Failed to schedule dispatch for batch {batch_id}: {exc}")


def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlng / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _route_distance_km(route: list, all_locs: list) -> float:
    loc_map = {}
    for loc in all_locs:
        key = loc.get('restaurant_id') or loc.get('order_id')
        if key:
            loc_map[key] = loc

    total = 0.0
    for i in range(len(route) - 1):
        k1 = route[i].get('restaurant_id') or route[i].get('order_id')
        k2 = route[i + 1].get('restaurant_id') or route[i + 1].get('order_id')
        l1, l2 = loc_map.get(k1), loc_map.get(k2)
        if l1 and l2:
            total += _haversine_km(l1['lat'], l1['lng'], l2['lat'], l2['lng'])
    return total


def _nearest_neighbour(pickups: list, deliveries: list) -> list:
    """Classic nearest-neighbour heuristic: all pickups first, then deliveries."""
    if not pickups or not deliveries:
        return pickups + deliveries

    route = []
    current_lat = pickups[0]['lat']
    current_lng = pickups[0]['lng']
    remaining_p = list(pickups)
    remaining_d = list(deliveries)

    while remaining_p or remaining_d:
        if remaining_p:
            nearest = min(
                remaining_p,
                key=lambda p: _haversine_km(current_lat, current_lng, p['lat'], p['lng'])
            )
            route.append(nearest)
            current_lat, current_lng = nearest['lat'], nearest['lng']
            remaining_p.remove(nearest)
        elif remaining_d:
            nearest = min(
                remaining_d,
                key=lambda d: _haversine_km(current_lat, current_lng, d['lat'], d['lng'])
            )
            route.append(nearest)
            current_lat, current_lng = nearest['lat'], nearest['lng']
            remaining_d.remove(nearest)

    return route


def _nearest_neighbour_single(locs: list) -> list:
    """Helper to optimize a single list of locations using nearest-neighbour."""
    if not locs:
        return []
    route = [locs[0]]
    remaining = list(locs[1:])
    current_lat = locs[0]['lat']
    current_lng = locs[0]['lng']
    while remaining:
        nearest = min(
            remaining,
            key=lambda l: _haversine_km(current_lat, current_lng, l['lat'], l['lng'])
        )
        route.append(nearest)
        current_lat, current_lng = nearest['lat'], nearest['lng']
        remaining.remove(nearest)
    return route


def _two_opt(route: list, all_locs: list) -> list:
    """
    2-opt improvement with a hard iteration cap.
    Fix #2: best_distance is computed ONCE per outer iteration, not once
    per pair. The old code recomputed _route_distance_km(best, ...) on
    every inner loop pair, making it O(n³) instead of O(n²) per iteration.
    """
    if len(route) < 4:
        return route

    best = list(route)
    for _ in range(MAX_2OPT_ITERATIONS):
        improved = False
        best_distance = _route_distance_km(best, all_locs)  # compute once per iteration
        for i in range(len(best) - 2):
            for j in range(i + 2, len(best)):
                candidate = best[:i + 1] + best[i + 1:j + 1][::-1] + best[j + 1:]
                candidate_distance = _route_distance_km(candidate, all_locs)
                if candidate_distance < best_distance:
                    best = candidate
                    best_distance = candidate_distance  # update for remaining pairs
                    improved = True
        if not improved:
            break

    return best
