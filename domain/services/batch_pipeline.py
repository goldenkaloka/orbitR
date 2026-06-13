"""
BatchPipeline — Single, unified batching entry point.

Replaces: BatchingEngine, BatchingService.

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
from django.utils import timezone

from apps.core.geo_utils import (
    get_zone_from_h3,
    get_zone_h3_cells,
    latlng_to_h3,
)

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
ZONE_LOCK_TTL = 60          # seconds — how long to hold the zone-level Redis lock
MAX_CANDIDATES = 50         # max orders to fetch per batching cycle
MAX_ORDERS_PER_BATCH = 5
MIN_ORDERS_FOR_BATCH = 2
NEIGHBOR_RING_K = 3         # H3 rings to expand from delivery cell for clustering
CLUSTER_RESOLUTION = 9      # fine resolution for delivery-proximity clustering
MAX_2OPT_ITERATIONS = 20    # hard cap — prevents unbounded loops inside transactions
CLUSTER_RING_K = 2          # H3 rings for delivery-proximity clustering
SINGLE_ORDER_GRACE_SECONDS = 30  # how long a lone order waits before getting its own batch

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

        # ── Pool order fast-path ───────────────────────────────────────────────
        # Pool orders already share a single delivery point by design — all
        # members' items are consolidated into one master order.  There is no
        # benefit in trying to cluster them with other orders; just create a
        # dedicated single-order batch immediately and dispatch it.
        if getattr(order, 'is_pool_order', False):
            logger.info(
                f"[BatchPipeline] Pool order {order.id} — skipping clustering, "
                f"creating dedicated single-order batch"
            )
            batch = BatchPipeline._create_single_order_batch_atomic(order)
            if batch:
                if order.pool_id:
                    from apps.orders.models import OrderPool
                    OrderPool.objects.filter(id=order.pool_id).update(batch_id=batch.id)
            return batch

        # ── Regular order path ────────────────────────────────────────────────
        # Check the order is inside an active operational zone, then attempt
        # to form a multi-order cluster.  If no cluster is found the order is
        # left for the poll cycle (batch_poll_task) which runs every N seconds
        # and forms denser batches across all pending orders in the zone.
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

    @staticmethod
    def run_for_pool(pool):
        """
        Create a dedicated batch for a pool and dispatch it immediately.
        Pools always dispatch as a single unit — no clustering with other orders.
        """
        if not pool.h3_index:
            logger.debug(f"[BatchPipeline] Pool {pool.id} has no h3_index — skipping")
            return None

        logger.info(f"[BatchPipeline] Creating dedicated batch for pool {pool.id}")
        return BatchPipeline._create_single_pool_batch(pool)


    # ─── Batch polling ────────────────────────────────────────────────────────

    @staticmethod
    def poll_all_zones():
        """
        Poll all active zones for READY, unbatched orders and form optimal batches.
        Called periodically by Celery beat.

        Designed as a complement to per-order triggering: this picks up orders
        that arrived shortly after earlier orders were already batched, forming
        denser multi-order clusters.
        """
        from apps.geo.models import OperationalZone

        zones = OperationalZone.objects.filter(is_active=True)
        total_batches = 0
        total_orders = 0

        for zone in zones:
            lock_key = f"batch_zone_lock:{zone.id}"
            acquired = cache.add(lock_key, "1", ZONE_LOCK_TTL)
            if not acquired:
                continue

            try:
                created = BatchPipeline._poll_zone(zone)
                total_batches += len(created)
                total_orders += sum(b.orders.count() for b in created if b)
            except Exception as exc:
                logger.error(
                    f"[BatchPipeline] Poll zone {zone.id} failed: {exc}",
                    exc_info=True,
                )
            finally:
                cache.delete(lock_key)

        if total_batches:
            logger.info(
                f"[BatchPipeline] Poll cycle complete — {total_batches} batch(es) "
                f"formed with {total_orders} order(s)"
            )
        return total_batches

    @staticmethod
    def _poll_zone(zone):
        """Collect and batch all READY unbatched orders in a single zone."""
        from apps.orders.models import Order
        from apps.core.geo_utils import get_zone_h3_cells

        zone_h3_cells = get_zone_h3_cells([zone.id])
        if not zone_h3_cells:
            return []

        candidates = list(
            Order.objects.filter(
                status='READY',
                h3_index__in=zone_h3_cells,
                batch__isnull=True,
            )
            .select_related('restaurant', 'customer')
            .order_by('ready_at')[:MAX_CANDIDATES]
        )

        if not candidates:
            return []

        created = []

        # ── Pass 1: Multi-order clusters ───────────────────────────────────────
        clusters = BatchPipeline._form_clusters_from_pool(candidates)
        batched_ids = set()
        for cluster in clusters:
            if len(cluster) < MIN_ORDERS_FOR_BATCH:
                continue
            route_dict = BatchPipeline._optimize_route(cluster)
            batch = BatchPipeline._create_multi_order_batch_atomic(
                cluster, route_dict, zone_id=zone.id
            )
            if batch:
                created.append(batch)
                for o in cluster:
                    batched_ids.add(o.id)

        # ── Pass 2: Stale singles (grace period expired) ───────────────────────
        remaining = [o for o in candidates if o.id not in batched_ids]
        cutoff = timezone.now() - timezone.timedelta(seconds=SINGLE_ORDER_GRACE_SECONDS)
        stale = [o for o in remaining if (o.ready_at or o.created_at) < cutoff]
        for order in stale:
            route_dict = BatchPipeline._optimize_route([order])
            batch = BatchPipeline._create_single_order_batch_atomic(order, route_dict=route_dict)
            if batch:
                created.append(batch)

        return created

    @staticmethod
    def _form_clusters_from_pool(candidates: list) -> list:
        """
        Form clusters of orders from the same restaurant.
        Single-restaurant only — no cross-restaurant batching.
        """
        from collections import defaultdict

        by_restaurant = defaultdict(list)
        for o in candidates:
            by_restaurant[o.restaurant_id].append(o)

        clusters = []
        batched_ids = set()

        for rest_id, orders in by_restaurant.items():
            by_customer = defaultdict(list)
            for o in orders:
                by_customer[o.customer_id].append(o)

            for cust_id, cust_orders in by_customer.items():
                cust_orders.sort(key=lambda o: o.ready_at or o.created_at)
                while cust_orders:
                    cluster = cust_orders[:MAX_ORDERS_PER_BATCH]
                    if len(cluster) >= MIN_ORDERS_FOR_BATCH:
                        clusters.append(cluster)
                        batched_ids.update(o.id for o in cluster)
                    cust_orders = cust_orders[MAX_ORDERS_PER_BATCH:]

            # Cross-customer orders from same restaurant batched together
            sorted_orders = sorted(orders, key=lambda o: o.ready_at or o.created_at)
            remaining = [o for o in sorted_orders if o.id not in batched_ids]
            while remaining:
                cluster = remaining[:MAX_ORDERS_PER_BATCH]
                if len(cluster) >= MIN_ORDERS_FOR_BATCH:
                    clusters.append(cluster)
                    batched_ids.update(o.id for o in cluster)
                remaining = remaining[MAX_ORDERS_PER_BATCH:]

        return clusters


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
            # No other orders — defer to poll cycle for denser batching
            logger.debug(
                f"[BatchPipeline] Order {order.id} has no candidates — "
                f"deferring to poll cycle"
            )
            return None

        # Try to form a multi-order cluster
        cluster = BatchPipeline._find_best_cluster(order, candidates)

        if cluster and len(cluster) >= MIN_ORDERS_FOR_BATCH:
            # Route optimization happens OUTSIDE the DB transaction
            route_dict = BatchPipeline._optimize_route(cluster)
            return BatchPipeline._create_multi_order_batch_atomic(
                cluster, route_dict, zone_id=zone.id
            )

        # No suitable cluster — defer to poll cycle for denser batching
        logger.debug(
            f"[BatchPipeline] Order {order.id} has no cluster — "
            f"deferring to poll cycle"
        )
        return None


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
        Find the best cluster of orders from the same restaurant.
        Single-restaurant batching only — no cross-restaurant.
        """
        same_restaurant = [
            o for o in candidates
            if o.restaurant_id == trigger_order.restaurant_id
        ]
        if len(same_restaurant) >= MIN_ORDERS_FOR_BATCH:
            same_restaurant.sort(key=lambda o: (
                0 if o.customer_id == trigger_order.customer_id else 1,
                o.ready_at or o.created_at,
            ))
            return _dedup_by_pk(same_restaurant)[:MAX_ORDERS_PER_BATCH]

        return None


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

        # Single-restaurant batch — no cross-restaurant discount
        batch_type = 'SINGLE_RESTAURANT'
        discount_rate = Decimal('0')

        # Pricing
        pricing = BatchPipeline._calculate_pricing(locked_orders)
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
            waiting_since=timezone.now(),
        )

        # Build delivery sequence map from route
        delivery_seq_map = _build_delivery_seq_map(route_dict)

        # Link orders to batch
        for order in locked_orders:
            order.batch = batch
            order.route_sequence = delivery_seq_map.get(str(order.id))

        Order.objects.bulk_update(locked_orders, ['batch', 'route_sequence'])

        # Pickup sequence — spatially sorted by nearest-first from first pickup
        BatchPipeline._create_batch_restaurants(batch, locked_orders, route_dict)

        # Fire dispatch after transaction commits
        batch_id_str = str(batch.id)
        transaction.on_commit(
            lambda: _schedule_dispatch(batch_id_str)
        )

        BatchPipeline._create_section_statuses_from_orders(batch, locked_orders)

        logger.info(
            f"[BatchPipeline] Created {batch_type} batch {batch.id} "
            f"orders={len(locked_orders)} distance={route_distance}km "
            f"payout={driver_payout} profit={company_profit}"
        )
        return batch

    @staticmethod
    def _create_single_pool_batch(pool, route_dict=None):
        """
        Create a single batch for a pool (no Order involvement).
        Pools always dispatch as a single unit.
        """
        from domain.services.mapbox_routing_service import MapboxRoutingService

        route_distance = Decimal('0')
        if route_dict is None:
            try:
                route = MapboxRoutingService.get_route_for_batch([pool])
                if route:
                    route_dict = route.to_dict() if hasattr(route, 'to_dict') else route
            except Exception as exc:
                logger.warning(f"[BatchPipeline] Mapbox failed for pool {pool.id}: {exc}")
        if route_dict and route_dict.get('total_distance_km'):
            route_distance = Decimal(str(route_dict['total_distance_km']))

        return BatchPipeline._persist_single_pool_batch(pool.id, pool, route_dict, route_distance)

    @staticmethod
    @transaction.atomic
    def _persist_single_pool_batch(pool_id, pool, route_dict, route_distance):
        """Atomically create a single batch for a pool."""
        from apps.orders.models import DeliveryBatch
        from domain.services.pricing_service import PricingService

        policy = PricingService.get_active_policy()
        min_guarantee = policy.driver_min_guarantee if policy else Decimal('200')
        per_km_rate = policy.driver_per_km_rate if policy else Decimal('50')
        per_stop_rate = policy.driver_per_stop_rate if policy else Decimal('30')

        delivery_fee = Decimal(str(pool.total_delivery_fee or 0))
        service_fee = Decimal(str(pool.service_fee or 0))
        user_paid_total = delivery_fee + service_fee

        driver_share = policy.single_order_driver_share if policy else Decimal('0.90')
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
            waiting_since=timezone.now(),
        )

        batch_id_str = str(batch.id)
        transaction.on_commit(lambda: _schedule_dispatch(batch_id_str))

        logger.info(
            f"[BatchPipeline] Created POOL batch {batch.id} for pool {pool_id} "
            f"distance={route_distance}km payout={driver_payout} profit={company_profit}"
        )
        return batch

    @staticmethod
    def _create_single_order_batch_atomic(order, route_dict=None):
        """
        Create a batch for a single order.

        Accepts an optional pre-computed route_dict to avoid a redundant
        Mapbox call when the caller already fetched it (e.g., the poll cycle).

        Fix #8: Mapbox network call is now OUTSIDE the DB transaction.
        The old version called MapboxRoutingService inside @transaction.atomic,
        holding row locks for the entire duration of the network call (2-5s).

        Steps:
          1. Optimistic existence check (no lock)
          2. Mapbox route call (outside any transaction) — skipped if route_dict provided
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
        route_distance = Decimal('0')
        if route_dict is None:
            try:
                route = MapboxRoutingService.get_route_for_batch([order])
                if route:
                    route_dict = route.to_dict() if hasattr(route, 'to_dict') else route
            except Exception as exc:
                logger.warning(f"[BatchPipeline] Mapbox failed for single order {order.id}: {exc}")
        if route_dict and route_dict.get('total_distance_km'):
            route_distance = Decimal(str(route_dict['total_distance_km']))

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
            waiting_since=timezone.now(),
        )

        locked_order.batch = batch
        locked_order.route_sequence = 1
        locked_order.save(update_fields=['batch', 'route_sequence'])

        batch_id_str = str(batch.id)
        transaction.on_commit(lambda: _schedule_dispatch(batch_id_str))

        logger.info(
            f"[BatchPipeline] Created SINGLE_ORDER batch {batch.id} "
            f"order={locked_order.id} distance={route_distance}km "
            f"payout={driver_payout} profit={company_profit}"
        )
        BatchPipeline._create_section_statuses_from_orders(batch, [locked_order])
        return batch


    # ─── Section statuses ─────────────────────────────────────────────────────

    @staticmethod
    def _create_section_statuses_from_orders(batch, orders):
        """Create BatchSectionStatus records for each unique section in orders."""
        from apps.orders.models import BatchSectionStatus, OrderItem

        section_ids = set()
        for order in orders:
            item_sections = (
                OrderItem.objects.filter(order=order)
                .exclude(section__isnull=True)
                .values_list('section_id', flat=True)
                .distinct()
            )
            section_ids.update(item_sections)

        for section_id in section_ids:
            BatchSectionStatus.objects.get_or_create(
                batch=batch,
                section_id=section_id,
                defaults={'status': 'PENDING'},
            )


    # ─── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _calculate_pricing(orders: list) -> dict:
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
            original_total += fee
            per_order[str(order.id)] = {'original': fee, 'discounted': fee}

        return {
            'original_total': original_total,
            'discounted_total': original_total,
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
