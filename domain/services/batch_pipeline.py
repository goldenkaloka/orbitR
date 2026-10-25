"""
BatchPipeline — New order-time batching system.

Pipeline:
  1. Order confirmed → create or join an ACCUMULATING batch
  2. ACCUMULATING → SOFT_LOCK (timer or max orders reached)
  3. SOFT_LOCK → FINALIZING (timer expired)
  4. FINALIZING → PRE_ASSIGNING (80%+ sections ready, driver pre-warm)
  5. FINALIZING/PRE_ASSIGNING → PENDING_DRIVER (all sections ready)
  6. PENDING_DRIVER → DISPATCHED (driver assigned)
  7. Auto: section becomes READY when last item in that section is marked ready
"""
from __future__ import annotations

import logging
import math
from decimal import Decimal

from django.db import models, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
ACCUMULATE_WINDOW_MINUTES = 10       # Default: 10 min in ACCUMULATING
SOFT_LOCK_WINDOW_SECONDS = 60        # 60s in SOFT_LOCK before FINALIZING
MAX_ORDERS_PER_BATCH = 5             # Hard limit
MIN_ORDERS_FOR_BATCH = 2             # Minimum to form a batch
PRE_ASSIGN_THRESHOLD = 0.8           # 80% sections ready → PRE_ASSIGNING
SECTION_TIMEOUT_MINUTES = 20         # If a section takes >20 min, escalate
PENDING_DRIVER_TIMEOUT_MINUTES = 60  # 1 hour in PENDING_DRIVER before expiry
MAX_ACCUMULATING_BATCHES_PER_RESTAURANT = 2  # Busy restaurants: max concurrent accumulating batches
BUSY_ACCUMULATE_WINDOW_MINUTES = 5   # Shorter window when restaurant is busy (≥2 batches)
PREORDER_BATCH_BUFFER_MINUTES = 2    # Slack after the slot's last release to let it join


class BatchPipeline:
    """
    New order-time batching entry point.
    Call BatchPipeline.on_order_confirmed(order) when a vendor confirms an order.
    """

    TERMINAL_ORDER_STATUSES = (
        'DELIVERED',
        'CANCELLED',
        'REFUNDED',
        'RESOLVED',
        'EXPIRED',
    )

    @staticmethod
    def complete_batch_if_finished(batch_id):
        """Move a batch to DELIVERED once every order is terminal.

        Called after a delivery (and by the lifecycle poll) so a batch does not
        stay DISPATCHED/PENDING_DRIVER forever when its orders are done.
        """
        from apps.orders.models import DeliveryBatch
        from domain.services.batch_fsm import BatchFSM

        try:
            with transaction.atomic():
                batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
                if batch.status in ('DELIVERED', 'CANCELLED', 'EXPIRED'):
                    return batch

                still_active = batch.orders.exclude(
                    status__in=BatchPipeline.TERMINAL_ORDER_STATUSES
                ).exists()
                if still_active:
                    return batch

                batch.status = BatchFSM.transition(batch, 'ALL_DELIVERED')
                batch.delivered_at = timezone.now()
                batch.save(update_fields=['status', 'delivered_at', 'updated_at'])
                logger.info(
                    f"[BatchPipeline] Batch {batch.id}: → DELIVERED "
                    "(all orders terminal)"
                )
                return batch
        except DeliveryBatch.DoesNotExist:
            return None

    @staticmethod
    def close_finished_batches():
        """Reconcile batches left open by deliveries/cancellations."""
        from apps.orders.models import DeliveryBatch

        batch_ids = DeliveryBatch.objects.exclude(
            status__in=['DELIVERED', 'CANCELLED', 'EXPIRED']
        ).values_list('id', flat=True)
        for batch_id in batch_ids:
            try:
                BatchPipeline.complete_batch_if_finished(batch_id)
            except Exception as exc:
                logger.error(
                    f"[BatchPipeline] Failed to close batch {batch_id}: {exc}"
                )


    @staticmethod
    def on_order_confirmed(order):
        """
        Called when an order is confirmed by the restaurant.
        Creates a new ACCUMULATING batch or joins an existing one.
        """
        from apps.orders.models import DeliveryBatch

        # Pre-orders are grouped by eating-time slot before any dispatch takes
        # place, so the 12:00 rush is known workload rather than a race.
        if order.scheduled_for:
            return BatchPipeline.on_preorder_released(order)

        order_h3 = order.h3_index
        if not order_h3:
            logger.warning(f"[BatchPipeline] Order {order.id} has no h3_index — creating single batch")
            return BatchPipeline._create_single_batch(order)

        # Look for an existing ACCUMULATING or SOFT_LOCK batch for this restaurant
        candidate = BatchPipeline._find_compatible_batch(order)
        if candidate:
            return BatchPipeline._join_batch(order, candidate)

        # ── Busy restaurant guard ────────────────────────────────────────
        # If restaurant already has max concurrent ACCUMULATING batches,
        # close the oldest one immediately to free up a kitchen slot
        # and create a new single batch for the new order.
        # This prevents the kitchen from being overwhelmed while avoiding
        # geographic mismatching detours.
        existing_count = DeliveryBatch.objects.filter(
            restaurant_id=order.restaurant_id,
            status='ACCUMULATING',
            preorder_slot_start__isnull=True,
        ).count()
        if existing_count >= MAX_ACCUMULATING_BATCHES_PER_RESTAURANT:
            oldest = (
                DeliveryBatch.objects.filter(
                    restaurant_id=order.restaurant_id,
                    status='ACCUMULATING',
                    preorder_slot_start__isnull=True,
                )
                .order_by('accumulating_since')
                .first()
            )
            if oldest:
                logger.info(
                    f"[BatchPipeline] Restaurant {order.restaurant_id} at max batches "
                    f"({existing_count}) — closing oldest batch {oldest.id} early to free a slot"
                )
                BatchPipeline._close_accumulating_batch(oldest.id)

        # No compatible batch found — create a new ACCUMULATING batch
        return BatchPipeline._create_single_batch(order)

    @staticmethod
    def on_preorder_released(order):
        """Put a released pre-order into its restaurant's slot batch.

        All pre-orders for the same restaurant and eating-time slot share one
        batch, regardless of which release sweep they arrive in. The batch
        stays open until the slot's last possible release so later bookings
        still land in the same unit of kitchen work.
        """
        from domain.services.orders.scheduled_orders import (
            release_at_for,
            slot_window_for,
        )

        slot_start, slot_end = slot_window_for(order.scheduled_for)
        last_release = release_at_for(order.restaurant, slot_end)
        now = timezone.now()
        window_minutes = max(
            ACCUMULATE_WINDOW_MINUTES,
            math.ceil((last_release - now).total_seconds() / 60)
            + PREORDER_BATCH_BUFFER_MINUTES,
        )

        candidate = BatchPipeline._find_preorder_batch(order, slot_start)
        if candidate:
            return BatchPipeline._join_batch(order, candidate)

        return BatchPipeline._create_single_batch(
            order,
            preorder_slot_start=slot_start,
            accumulate_window_minutes=window_minutes,
        )

    @staticmethod
    def _find_preorder_batch(order, slot_start):
        """The open batch for this restaurant and slot, if it has room."""
        from apps.orders.models import DeliveryBatch

        batches = (
            DeliveryBatch.objects.filter(
                restaurant_id=order.restaurant_id,
                preorder_slot_start=slot_start,
                status__in=['ACCUMULATING', 'SOFT_LOCK'],
            )
            .annotate(order_count=models.Count('orders'))
            .filter(order_count__lt=MAX_ORDERS_PER_BATCH)
            .order_by('accumulating_since')
        )

        for batch in batches:
            if BatchPipeline._batch_is_compatible(
                order, batch, is_soft_lock=batch.status == 'SOFT_LOCK',
            ):
                return batch
        return None

    @staticmethod
    def on_pool_ready(pool):
        """
        Create a dedicated batch for a pool. Pools always dispatch as a single unit.
        """
        return BatchPipeline._create_pool_batch(pool)

    @staticmethod
    def _find_compatible_batch(order):
        """
        Find an existing ACCUMULATING or SOFT_LOCK batch that can accept this order.

        Platform-managed restaurants may join a batch shared with other
        platform-managed restaurants. Partner restaurants are pinned to their
        own restaurant's batches — see
        Restaurant.allows_multi_restaurant_batching.
        """
        from apps.orders.models import DeliveryBatch, Order
        from apps.restaurants.models import Restaurant

        if order.restaurant and order.restaurant.allows_multi_restaurant_batching:
            pool = list(
                Restaurant.objects
                .filter(source_type=Restaurant.SOURCE_PLATFORM_MANAGED)
                .values_list('id', flat=True)
            )
        else:
            pool = [order.restaurant_id]

        # Prefer ACCUMULATING first, then SOFT_LOCK. Slot batches are reserved
        # for pre-orders so live orders cannot eat into the promised workload.
        accumulating = list(
            DeliveryBatch.objects.filter(
                restaurant_id__in=pool,
                status='ACCUMULATING',
                preorder_slot_start__isnull=True,
            )
            .annotate(order_count=models.Count('orders'))
            .filter(order_count__lt=MAX_ORDERS_PER_BATCH)
            .order_by('accumulating_since')
        )

        for batch in accumulating:
            if BatchPipeline._batch_is_compatible(order, batch):
                return batch

        soft_lock = list(
            DeliveryBatch.objects.filter(
                restaurant_id__in=pool,
                status='SOFT_LOCK',
                preorder_slot_start__isnull=True,
            )
            .annotate(order_count=models.Count('orders'))
            .filter(order_count__lt=MAX_ORDERS_PER_BATCH)
            .order_by('soft_lock_at')
        )

        for batch in soft_lock:
            if BatchPipeline._batch_is_compatible(order, batch, is_soft_lock=True):
                return batch

        return None

    @staticmethod
    def _batch_is_compatible(order, batch, is_soft_lock=False):
        """
        Compatibility check for a candidate batch.

        For platform-managed orders this also refuses a batch that already
        contains a partner restaurant, so a pooled batch can never pick up an
        order it is not allowed to serve.
        """
        from apps.restaurants.models import Restaurant

        if not BatchPipeline._is_compatible(order, batch, is_soft_lock):
            return False

        if not (order.restaurant and order.restaurant.allows_multi_restaurant_batching):
            return True

        existing_ids = set(batch.orders.values_list('restaurant_id', flat=True))
        if batch.restaurant_id:
            existing_ids.add(batch.restaurant_id)
        existing_ids.discard(order.restaurant_id)

        if not existing_ids:
            return True

        eligible = Restaurant.objects.filter(
            id__in=existing_ids,
            source_type=Restaurant.SOURCE_PLATFORM_MANAGED,
        ).count()

        return eligible == len(existing_ids)

    @staticmethod
    def _sync_batch_restaurants(batch):
        """
        Keep BatchRestaurant rows (with pickup sequence) in step with the orders
        actually in the batch.

        Pickup order follows each restaurant's first order's route_sequence, so
        the driver visits restaurants in the order the route already implies.
        """
        from apps.orders.models import BatchRestaurant, Order
        from apps.restaurants.models import Restaurant

        orders = list(
            batch.orders.select_related('restaurant').order_by('route_sequence')
        )

        sequence_by_restaurant = {}
        for order in orders:
            if not order.restaurant_id:
                continue
            if order.restaurant_id not in sequence_by_restaurant:
                sequence_by_restaurant[order.restaurant_id] = len(sequence_by_restaurant) + 1

        existing = {
            br.restaurant_id: br
            for br in BatchRestaurant.objects.filter(batch=batch)
        }

        for restaurant_id, sequence in sequence_by_restaurant.items():
            row = existing.get(restaurant_id)
            if row:
                if row.pickup_sequence != sequence:
                    row.pickup_sequence = sequence
                    row.save(update_fields=['pickup_sequence'])
            else:
                BatchRestaurant.objects.create(
                    batch=batch,
                    restaurant_id=restaurant_id,
                    pickup_sequence=sequence,
                )

        for restaurant_id, row in existing.items():
            if restaurant_id not in sequence_by_restaurant:
                row.delete()

        distinct = len(sequence_by_restaurant)
        is_multi = distinct > 1

        # zone_ids / is_inter_zone are informational here: they record the
        # distinct pickup cells the batch spans so an admin can see at a glance
        # whether a pooled batch is a cross-town run. Nothing in dispatch
        # currently branches on them.
        restaurant_h3 = [
            h3 for h3 in (
                Restaurant.objects
                .filter(id__in=sequence_by_restaurant.keys())
                .values_list('h3_index', flat=True)
            ) if h3
        ]

        batch.batch_type = 'MULTI_RESTAURANT' if is_multi else 'SINGLE_RESTAURANT'
        batch.zone_ids = sorted(set(restaurant_h3))
        batch.is_inter_zone = is_multi and len(set(restaurant_h3)) > 1
        batch.save(update_fields=['batch_type', 'zone_ids', 'is_inter_zone', 'updated_at'])
        return distinct

    @staticmethod
    def _is_compatible(order, batch, is_soft_lock=False):
        """
        Check if an order is compatible with a batch.
        Criteria: H3 delivery proximity via disk rings.
        ACCUMULATING: k=3 ring. SOFT_LOCK: k=1 ring (stricter).
        Must be compatible with ALL orders in the batch to prevent route divergence.
        """
        import h3

        if not order.h3_index:
            return True

        ring_k = 1 if is_soft_lock else 3

        for existing_order in batch.orders.all():
            if existing_order.h3_index:
                try:
                    dist = h3.grid_distance(order.h3_index, existing_order.h3_index)
                    if dist > ring_k:
                        return False
                except Exception:
                    # Fallback to first-order check if grid_distance fails
                    if existing_order == batch.orders.first():
                        return False

        return True

    @staticmethod
    def _join_batch(order, batch):
        """
        Add an order to an existing batch atomically.
        """
        from apps.orders.models import DeliveryBatch, Order

        with transaction.atomic():
            locked_batch = DeliveryBatch.objects.select_for_update().get(id=batch.id)
            if locked_batch.status not in ('ACCUMULATING', 'SOFT_LOCK'):
                logger.warning(f"[BatchPipeline] Batch {batch.id} status '{locked_batch.status}' — cannot join")
                return None
            if locked_batch.orders.count() >= MAX_ORDERS_PER_BATCH:
                logger.warning(f"[BatchPipeline] Batch {batch.id} is full — cannot join")
                return None

            order.batch = locked_batch
            order.route_sequence = locked_batch.orders.count() + 1
            order.save(update_fields=['batch', 'route_sequence'])
            locked_batch.last_order_confirmed_at = timezone.now()

            # Update pricing incrementally
            locked_batch.user_paid_total += Decimal(str(order.delivery_fee or 0)) + Decimal(str(order.service_fee or 0))
            locked_batch.original_total_fee += Decimal(str(order.delivery_fee or 0))
            locked_batch.discounted_total_fee += Decimal(str(order.delivery_fee or 0))
            # If the batch was already in SOFT_LOCK, extend the soft lock by 30 seconds
            # to give the kitchen a buffer to prepare the new last-minute order.
            update_fields = [
                'last_order_confirmed_at',
                'user_paid_total', 'original_total_fee', 'discounted_total_fee',
            ]
            if locked_batch.status == 'SOFT_LOCK' and locked_batch.soft_lock_at:
                locked_batch.soft_lock_at += timezone.timedelta(seconds=30)
                update_fields.append('soft_lock_at')

            locked_batch.save(update_fields=update_fields)

            BatchPipeline._create_section_statuses_from_orders(locked_batch, [order])
            restaurant_count = BatchPipeline._sync_batch_restaurants(locked_batch)

        if restaurant_count > 1:
            logger.info(
                f"[BatchPipeline] Order {order.id} joined multi-restaurant batch "
                f"{batch.id} ({restaurant_count} restaurants, "
                f"type={locked_batch.batch_type})"
            )

        logger.info(
            f"[BatchPipeline] Order {order.id} joined batch {batch.id} "
            f"(status={locked_batch.status}, total_orders={locked_batch.orders.count()})"
        )

        # If batch reached max orders while ACCUMULATING, close it
        if locked_batch.status == 'ACCUMULATING' and locked_batch.orders.count() >= MAX_ORDERS_PER_BATCH:
            BatchPipeline._close_accumulating_batch(locked_batch.id)

        return locked_batch

    @staticmethod
    def _create_single_batch(
        order,
        preorder_slot_start=None,
        accumulate_window_minutes=ACCUMULATE_WINDOW_MINUTES,
    ):
        """
        Create a new ACCUMULATING batch with this order as the first member.
        """
        from apps.orders.models import DeliveryBatch, Order

        now = timezone.now()

        with transaction.atomic():
            locked_order = (
                Order.objects.select_for_update(skip_locked=True)
                .filter(id=order.id, batch__isnull=True)
                .first()
            )
            if not locked_order:
                logger.info(f"[BatchPipeline] Order {order.id} already batched by another worker")
                return None

            batch = DeliveryBatch.objects.create(
                batch_type='SINGLE_RESTAURANT',
                restaurant_id=locked_order.restaurant_id,
                status='ACCUMULATING',
                accumulating_since=now,
                first_order_confirmed_at=now,
                last_order_confirmed_at=now,
                user_paid_total=Decimal(str(locked_order.delivery_fee or 0)) + Decimal(str(locked_order.service_fee or 0)),
                original_total_fee=Decimal(str(locked_order.delivery_fee or 0)),
                discounted_total_fee=Decimal(str(locked_order.delivery_fee or 0)),
                driver_payout=Decimal('0'),
                company_profit=Decimal('0'),
                offer_attempt=0,
                max_orders=MAX_ORDERS_PER_BATCH,
                accumulate_window_minutes=accumulate_window_minutes,
                preorder_slot_start=preorder_slot_start,
            )

            locked_order.batch = batch
            locked_order.route_sequence = 1
            locked_order.save(update_fields=['batch', 'route_sequence'])

            BatchPipeline._create_section_statuses_from_orders(batch, [locked_order])
            BatchPipeline._sync_batch_restaurants(batch)

            batch_id_str = str(batch.id)
            from domain.services.dispatch_tasks import close_accumulating_batch_task
            transaction.on_commit(
                lambda: close_accumulating_batch_task.apply_async(
                    args=[batch_id_str],
                    countdown=accumulate_window_minutes * 60
                )
            )

        logger.info(
            f"[BatchPipeline] Created ACCUMULATING batch {batch.id} for order {order.id}"
            + (f" (slot {preorder_slot_start.isoformat()})" if preorder_slot_start else "")
        )
        return batch

    @staticmethod
    def _close_accumulating_batch(batch_id: str):
        """
        Move a batch from ACCUMULATING → SOFT_LOCK.
        Called by timer task or when max orders reached.
        """
        from apps.orders.models import DeliveryBatch
        from domain.services.batch_fsm import BatchFSM

        try:
            with transaction.atomic():
                batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
                if batch.status != 'ACCUMULATING':
                    return
                batch.status = BatchFSM.transition(batch, 'TIMER_SOFT_LOCK')
                batch.soft_lock_at = timezone.now()
                batch.save(update_fields=['status', 'soft_lock_at', 'updated_at'])
            logger.info(f"[BatchPipeline] Batch {batch_id}: ACCUMULATING → SOFT_LOCK ({batch.orders.count()} orders)")
        except Exception as exc:
            logger.error(f"[BatchPipeline] Failed to close accumulating batch {batch_id}: {exc}")

    @staticmethod
    def _finalize_batch(batch_id: str):
        """
        Move a batch from SOFT_LOCK → FINALIZING.
        Called by timer task when SOFT_LOCK window expires.
        Computes optimal route and recalculates pricing OUTSIDE the
        DB transaction (no locks held during Mapbox network calls).
        """
        from apps.orders.models import DeliveryBatch, Order
        from domain.services.batch_fsm import BatchFSM
        from domain.services.mapbox_routing_service import MapboxRoutingService

        # Step 1: Fetch orders and compute route outside transaction
        try:
            batch_pre = DeliveryBatch.objects.get(id=batch_id)
            if batch_pre.status != 'SOFT_LOCK':
                return

            orders = list(batch_pre.orders.select_related('restaurant').all())
            if not orders:
                logger.warning(f"[BatchPipeline] Batch {batch_id} has no orders — skipping finalize")
                return

            route_dict = None
            route_distance = Decimal('0')
            try:
                route = MapboxRoutingService.get_route_for_batch(orders)
                if route:
                    route_dict = route.to_dict() if hasattr(route, 'to_dict') else route
            except Exception as exc:
                logger.warning(f"[BatchPipeline] Mapbox routing failed for batch {batch_id}: {exc}")

            if route_dict and route_dict.get('total_distance_km'):
                route_distance = Decimal(str(route_dict['total_distance_km']))

            # Recalculate pricing with all orders now known
            pricing = BatchPipeline._calculate_pricing(orders)
            driver_payout, user_paid_total = BatchPipeline._calculate_driver_payout(
                orders, route_dict, pricing
            )
            company_profit = max(Decimal('0'), user_paid_total - driver_payout)

            # Step 2: Persist atomically inside transaction
            with transaction.atomic():
                batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
                if batch.status != 'SOFT_LOCK':
                    return
                batch.status = BatchFSM.transition(batch, 'TIMER_FINALIZE')
                batch.finalized_at = timezone.now()
                batch.route_optimization = route_dict
                batch.route_distance_km = route_distance
                batch.user_paid_total = user_paid_total
                batch.original_total_fee = pricing['original_total']
                batch.discounted_total_fee = pricing['discounted_total']
                batch.driver_payout = driver_payout
                batch.company_profit = company_profit
                batch.save(update_fields=[
                    'status', 'finalized_at', 'route_optimization', 'route_distance_km',
                    'user_paid_total', 'original_total_fee', 'discounted_total_fee',
                    'driver_payout', 'company_profit', 'updated_at',
                ])

                # Update route sequence for each order
                delivery_seq_map = _build_delivery_seq_map(route_dict)
                for order in orders:
                    seq = delivery_seq_map.get(str(order.id))
                    if seq is not None:
                        Order.objects.filter(id=order.id).update(route_sequence=seq)

            logger.info(
                f"[BatchPipeline] Batch {batch_id}: SOFT_LOCK → FINALIZING "
                f"({len(orders)} orders, {route_distance}km, payout={driver_payout})"
            )
        except Exception as exc:
            logger.error(f"[BatchPipeline] Failed to finalize batch {batch_id}: {exc}", exc_info=True)

    @staticmethod
    def _try_pre_assign(batch):
        """
        Check if the batch meets the pre-assignment threshold (80%+ sections ready).
        If so, move to PRE_ASSIGNING and ping drivers early.
        """
        from domain.services.batch_fsm import BatchFSM
        from domain.services.dispatch_tasks import pre_assign_drivers_for_batch

        try:
            with transaction.atomic():
                batch_ref = batch.__class__.objects.select_for_update().get(id=batch.id)
                if batch_ref.status not in ('FINALIZING', 'PARTIAL_BATCH'):
                    return
                pct = BatchFSM.sections_ready_pct(batch_ref)
                if pct >= PRE_ASSIGN_THRESHOLD:
                    batch_ref.status = BatchFSM.transition(batch_ref, 'THRESHOLD_PRE_ASSIGN')
                    batch_ref.pre_assigned_at = timezone.now()
                    batch_ref.save(update_fields=['status', 'pre_assigned_at', 'updated_at'])
                    transaction.on_commit(
                        lambda bid=str(batch_ref.id): pre_assign_drivers_for_batch.delay(bid)
                    )
                    logger.info(f"[BatchPipeline] Batch {batch_ref.id}: → PRE_ASSIGNING ({pct:.0%} sections ready)")
        except Exception as exc:
            logger.error(f"[BatchPipeline] Pre-assign check failed for batch {batch.id}: {exc}")

    @staticmethod
    def _check_all_sections_ready(batch):
        """
        Check if all sections for a batch are READY.
        If so, transition to PENDING_DRIVER.
        """
        from domain.services.batch_fsm import BatchFSM
        from domain.services.dispatch_tasks import dispatch_batch_task

        try:
            with transaction.atomic():
                batch_ref = batch.__class__.objects.select_for_update().get(id=batch.id)
                if not BatchFSM.sections_all_ready(batch_ref):
                    return
                if batch_ref.status in ('ACCUMULATING', 'SOFT_LOCK', 'FINALIZING', 'PRE_ASSIGNING', 'PARTIAL_BATCH'):
                    # Recalculate route and pricing if not finalized yet (driver_payout is 0)
                    update_fields = ['status', 'waiting_since', 'updated_at']
                    if batch_ref.driver_payout == Decimal('0'):
                        from domain.services.mapbox_routing_service import MapboxRoutingService
                        orders = list(batch_ref.orders.all())
                        route_dict = None
                        route_distance = Decimal('0')
                        try:
                            route = MapboxRoutingService.get_route_for_batch(orders)
                            if route:
                                route_dict = route.to_dict() if hasattr(route, 'to_dict') else route
                        except Exception as exc:
                            logger.warning(f"[BatchPipeline] Mapbox routing failed during all-ready check: {exc}")

                        if route_dict and route_dict.get('total_distance_km'):
                            route_distance = Decimal(str(route_dict['total_distance_km']))

                        pricing = BatchPipeline._calculate_pricing(orders)
                        driver_payout, user_paid_total = BatchPipeline._calculate_driver_payout(
                            orders, route_dict, pricing
                        )
                        company_profit = max(Decimal('0'), user_paid_total - driver_payout)

                        batch_ref.route_optimization = route_dict
                        batch_ref.route_distance_km = route_distance
                        batch_ref.user_paid_total = user_paid_total
                        batch_ref.original_total_fee = pricing['original_total']
                        batch_ref.discounted_total_fee = pricing['discounted_total']
                        batch_ref.driver_payout = driver_payout
                        batch_ref.company_profit = company_profit
                        
                        update_fields.extend([
                            'route_optimization', 'route_distance_km', 'user_paid_total',
                            'original_total_fee', 'discounted_total_fee', 'driver_payout',
                            'company_profit'
                        ])

                        # Save routing sequence for orders
                        delivery_seq_map = _build_delivery_seq_map(route_dict)
                        for order in orders:
                            seq = delivery_seq_map.get(str(order.id))
                            if seq is not None:
                                order.route_sequence = seq
                                order.save(update_fields=['route_sequence'])

                    batch_ref.status = BatchFSM.transition(batch_ref, 'ALL_SECTIONS_READY')
                    batch_ref.waiting_since = timezone.now()
                    batch_ref.save(update_fields=update_fields)
                    transaction.on_commit(
                        lambda bid=str(batch_ref.id): dispatch_batch_task.delay(bid)
                    )
                    logger.info(f"[BatchPipeline] Batch {batch_ref.id}: → PENDING_DRIVER (all sections ready)")
        except Exception as exc:
            logger.error(f"[BatchPipeline] All-sections-ready check failed for batch {batch.id}: {exc}")


    # ─── Section auto-readiness ───────────────────────────────────────────────

    @staticmethod
    def on_order_marked_ready(order):
        """
        Called when an order is marked READY (cooked).
        Auto-updates BatchSectionStatus for sections where ALL items are now ready.
        If all sections become READY, triggers batch progression.
        """
        from apps.orders.models import BatchSectionStatus, OrderItem

        batch = order.batch
        if not batch:
            logger.debug(f"[BatchPipeline] Order {order.id} has no batch — skipping section auto-ready")
            return

        order_item_sections = set(
            OrderItem.objects.filter(order=order)
            .exclude(section__isnull=True)
            .values_list('section_id', flat=True)
        )
        if not order_item_sections:
            return

        for section_id in order_item_sections:
            bss = BatchSectionStatus.objects.filter(
                batch=batch,
                section_id=section_id,
            ).first()
            if not bss or bss.status == 'READY':
                continue

            unready_items = OrderItem.objects.filter(
                order__batch=batch,
                section_id=section_id,
                order__status__in=['CONFIRMED', 'PREPARING'],
            ).exclude(order__status='READY').exists()

            if not unready_items:
                bss.status = 'READY'
                bss.updated_at = timezone.now()
                bss.save(update_fields=['status', 'updated_at'])
                logger.info(
                    f"[BatchPipeline] Auto-marked section {section_id} READY "
                    f"in batch {batch.id} (last item cooked)"
                )

                # Check if we should pre-assign or go to PENDING_DRIVER
                BatchPipeline._check_all_sections_ready(batch)
                BatchPipeline._try_pre_assign(batch)

    @staticmethod
    def on_section_started_prepping(batch_id, section_id):
        """
        Called when a section starts cooking items from this batch.
        Updates BSS to PREPARING and sets estimated_ready_at.
        """
        from apps.orders.models import BatchSectionStatus
        from django.utils import timezone

        from apps.restaurants.models import RestaurantSection
        section = RestaurantSection.objects.only('prep_time').filter(id=section_id).first()
        prep_minutes = section.prep_time if section and section.prep_time else 15

        updated = BatchSectionStatus.objects.filter(
            batch_id=batch_id,
            section_id=section_id,
            status='PENDING',
        ).update(
            status='PREPARING',
            started_prepping_at=timezone.now(),
            estimated_ready_at=timezone.now() + timezone.timedelta(minutes=prep_minutes),
        )
        if updated:
            logger.info(f"[BatchPipeline] Section {section_id} in batch {batch_id}: PENDING → PREPARING")


    # ─── Pool batching ────────────────────────────────────────────────────────

    @staticmethod
    def _create_pool_batch(pool, route_dict=None):
        """Create a batch for a pool and dispatch it immediately."""
        from apps.orders.models import DeliveryBatch, Order
        from domain.services.mapbox_routing_service import MapboxRoutingService
        from domain.services.orders.scheduled_orders import slot_window_for

        # A pre-order bucket is delivered for its booked slot; tagging the
        # batch keeps it out of live-order sweeps and ties it to that meal.
        preorder_slot_start = None
        if getattr(pool, 'scheduled_for', None):
            preorder_slot_start, _ = slot_window_for(pool.scheduled_for)

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

        with transaction.atomic():
            from domain.services.pricing_service import PricingService
            policy = PricingService.get_active_policy()
            min_guarantee = policy.driver_min_guarantee if policy else Decimal('200')
            per_km_rate = policy.driver_per_km_rate if policy else Decimal('50')
            per_stop_rate = policy.driver_per_stop_rate if policy else Decimal('30')

            delivery_fee = Decimal(str(pool.total_delivery_fee or 0))
            service_fee = Decimal(str(pool.service_fee or 0))
            user_paid_total = delivery_fee + service_fee

            driver_share = policy.single_order_driver_share if policy else Decimal('0.95')
            driver_payout = delivery_fee * driver_share
            if route_distance > 0:
                calc = min_guarantee + (route_distance * per_km_rate) + (Decimal('2') * per_stop_rate)
                driver_payout = max(driver_payout, calc)
            driver_payout = min(driver_payout, user_paid_total)
            driver_payout = max(driver_payout, Decimal('0'))
            company_profit = user_paid_total - driver_payout

            batch = DeliveryBatch.objects.create(
                batch_type='SINGLE_RESTAURANT',
                restaurant_id=pool.restaurant_id,
                status='PENDING_DRIVER',
                preorder_slot_start=preorder_slot_start,
                route_optimization=route_dict,
                route_distance_km=route_distance,
                user_paid_total=user_paid_total,
                original_total_fee=delivery_fee,
                discounted_total_fee=delivery_fee,
                driver_payout=driver_payout,
                company_profit=company_profit,
                offer_attempt=0,
                waiting_since=timezone.now(),
                accumulating_since=timezone.now(),
                finalized_at=timezone.now(),
            )

            pool_orders = list(Order.objects.filter(pool_id=pool.id))
            for i, order in enumerate(pool_orders, 1):
                order.batch = batch
                order.status = 'READY'
                order.route_sequence = i
                order.save(update_fields=['batch', 'status', 'route_sequence'])

            BatchPipeline._create_section_statuses_from_orders(batch, pool_orders)

            batch_id_str = str(batch.id)
            transaction.on_commit(lambda: _schedule_dispatch(batch_id_str))

        logger.info(
            f"[BatchPipeline] Created POOL batch {batch.id} for pool {pool.id} "
            f"distance={route_distance}km"
        )
        return batch


    # ─── Section status helpers ───────────────────────────────────────────────

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

    @staticmethod
    def check_and_close_full_batches():
        """
        Periodic check: close ACCUMULATING batches that have hit max orders.
        """
        from apps.orders.models import DeliveryBatch

        full_batches = DeliveryBatch.objects.filter(
            status='ACCUMULATING',
        ).annotate(
            order_count=models.Count('orders')
        ).filter(
            order_count__gte=MAX_ORDERS_PER_BATCH,
        )
        for batch in full_batches:
            BatchPipeline._close_accumulating_batch(str(batch.id))

    @staticmethod
    def check_and_expire_accumulating():
        """
        Periodic check: move ACCUMULATING batches past their window to SOFT_LOCK.
        Busy restaurants (≥2 concurrent batches) use a shorter window.
        Pre-order slot batches use their own deadline (the end of the slot) so
        they are never closed or cancelled before every slot booking released.
        Also cleans up stale ACCUMULATING batches where no order has progressed
        to PREPARING or READY after more than 2x the accumulate window.
        Uses efficient batch-queries — never loads all batches into Python memory.
        """
        from apps.orders.models import DeliveryBatch
        from django.db.models import Count, Exists, OuterRef

        now = timezone.now()

        # ── Pass 1: Batches past their normal accumulate window → SOFT_LOCK ──
        expired = DeliveryBatch.objects.filter(
            status='ACCUMULATING',
            preorder_slot_start__isnull=True,
            accumulating_since__isnull=False,
            accumulating_since__lt=now - timezone.timedelta(minutes=ACCUMULATE_WINDOW_MINUTES),
        ).only('id')
        for batch in expired:
            BatchPipeline._close_accumulating_batch(str(batch.id))

        # ── Pass 1b: Busy restaurants — shorter window ──
        busy = (
            DeliveryBatch.objects.filter(
                status='ACCUMULATING',
                preorder_slot_start__isnull=True,
            )
            .values('restaurant_id')
            .annotate(cnt=Count('id'))
            .filter(cnt__gte=MAX_ACCUMULATING_BATCHES_PER_RESTAURANT)
        )
        busy_restaurant_ids = [b['restaurant_id'] for b in busy]
        if busy_restaurant_ids:
            busy_expired = DeliveryBatch.objects.filter(
                restaurant_id__in=busy_restaurant_ids,
                status='ACCUMULATING',
                preorder_slot_start__isnull=True,
                accumulating_since__isnull=False,
                accumulating_since__lt=now - timezone.timedelta(minutes=BUSY_ACCUMULATE_WINDOW_MINUTES),
            ).only('id')
            for batch in busy_expired:
                BatchPipeline._close_accumulating_batch(str(batch.id))

        # ── Pass 1c: pre-order slot batches use their own deadline ──
        # They span the whole slot so late bookings can join, which is longer
        # than the generic windows above.
        preorder_open = DeliveryBatch.objects.filter(
            status='ACCUMULATING',
            preorder_slot_start__isnull=False,
            accumulating_since__isnull=False,
        ).only('id', 'accumulating_since', 'accumulate_window_minutes')
        for batch in preorder_open:
            deadline = batch.accumulating_since + timezone.timedelta(
                minutes=batch.accumulate_window_minutes,
            )
            if now >= deadline:
                BatchPipeline._close_accumulating_batch(str(batch.id))

        # ── Pass 2: Batches > 2x window with no cooking progress → cancel ──
        from apps.orders.models import Order
        has_progress = Exists(
            Order.objects.filter(
                batch=OuterRef('pk'),
                status__in=['PREPARING', 'READY'],
            )
        )
        stale_no_progress = DeliveryBatch.objects.filter(
            status='ACCUMULATING',
            preorder_slot_start__isnull=True,
            accumulating_since__isnull=False,
            accumulating_since__lt=now - timezone.timedelta(minutes=ACCUMULATE_WINDOW_MINUTES * 2),
        ).annotate(
            has_progress=has_progress
        ).filter(has_progress=False).only('id')

        for batch in stale_no_progress:
            logger.warning(f"[BatchPipeline] Batch {batch.id} stale with no cooking progress — cancelling")
            from domain.services.batch_fsm import BatchFSM
            try:
                with transaction.atomic():
                    locked = DeliveryBatch.objects.select_for_update().get(id=batch.id)
                    if locked.status == 'ACCUMULATING':
                        locked.status = BatchFSM.transition(locked, 'CANCEL')
                        locked.save(update_fields=['status', 'updated_at'])
            except Exception as exc:
                logger.error(f"[BatchPipeline] Failed to cancel stale batch {batch.id}: {exc}")

    @staticmethod
    def check_and_expire_soft_lock():
        """
        Periodic check: move SOFT_LOCK batches past their window to FINALIZING.
        """
        from apps.orders.models import DeliveryBatch

        cutoff = timezone.now() - timezone.timedelta(seconds=SOFT_LOCK_WINDOW_SECONDS)
        stale = DeliveryBatch.objects.filter(
            status='SOFT_LOCK',
            soft_lock_at__lt=cutoff,
        )
        for batch in stale:
            BatchPipeline._finalize_batch(str(batch.id))

    @staticmethod
    def check_section_timeouts():
        """
        Periodic check: find sections stuck in PENDING/PREPARING past timeout.
        Escalates by transitioning the batch to PARTIAL_BATCH.
        Uses the bss_status_updated_idx index for efficient filtering.
        """
        from apps.orders.models import BatchSectionStatus, DeliveryBatch
        from domain.services.batch_fsm import BatchFSM

        cutoff = timezone.now() - timezone.timedelta(minutes=SECTION_TIMEOUT_MINUTES)
        stuck = BatchSectionStatus.objects.filter(
            status__in=['PENDING', 'PREPARING'],
            updated_at__lt=cutoff,
            batch__status__in=['FINALIZING', 'PRE_ASSIGNING', 'SOFT_LOCK', 'PARTIAL_BATCH'],
        ).select_related('batch').only('id', 'section_id', 'batch_id', 'batch__status')

        for bss in stuck:
            try:
                escalated = False
                with transaction.atomic():
                    batch = DeliveryBatch.objects.select_for_update().get(id=bss.batch_id)
                    previous_status = batch.status
                    if batch.status in ('FINALIZING', 'SOFT_LOCK', 'PARTIAL_BATCH'):
                        batch.status = BatchFSM.transition(batch, 'SECTION_FAIL')
                        batch.save(update_fields=['status', 'updated_at'])
                        # Only escalate on the *first* entry into PARTIAL_BATCH.
                        # A batch already in PARTIAL_BATCH stays there (SECTION_FAIL
                        # is a self-loop), so alerting on every poll would re-send
                        # the push/SMS to admins every cycle — indefinitely.
                        escalated = (
                            previous_status != 'PARTIAL_BATCH'
                            and batch.status == 'PARTIAL_BATCH'
                        )
                    logger.warning(
                        f"[BatchPipeline] Section {bss.section_id} in batch {bss.batch_id} "
                        f"stuck for >{SECTION_TIMEOUT_MINUTES}min — batch → {batch.status}"
                    )

                # PARTIAL_BATCH is a holding state with no automatic recovery:
                # the batch will not dispatch until the stuck section is marked
                # READY. Alert an admin so a human can chase the kitchen or
                # cancel, instead of the run parking silently.
                if escalated:
                    BatchPipeline._alert_stalled_batch(batch, bss)
            except Exception as exc:
                logger.error(f"[BatchPipeline] Section timeout escalation failed: {exc}")

    @staticmethod
    def _alert_stalled_batch(batch, bss):
        """Tell admins a batch is held up by a section that never went ready."""
        from django.core.cache import cache
        from infrastructure.notifications.messages import admin_batch_stalled
        from infrastructure.notifications.service import NotificationService

        # De-dupe guard: the stalled state never self-recovers, so repeating the
        # alert (every poll cycle, or from racing workers) is pure noise. One
        # alert per batch is enough; admins chase it from the dashboard.
        dedupe_key = f"batch_stalled_alerted:{batch.id}"
        if not cache.add(dedupe_key, 1, timeout=6 * 3600):
            logger.info(
                f"[BatchPipeline] Stalled alert already sent for batch {batch.id} — skipping"
            )
            return

        try:
            restaurant_ids = list(
                batch.orders.values_list('restaurant_id', flat=True).distinct()
            )
            title, body = admin_batch_stalled(
                str(batch.id)[:8].upper(),
                len(restaurant_ids),
                SECTION_TIMEOUT_MINUTES,
            )
            NotificationService.notify_admins(
                title=title,
                body=body,
                sms=True,
                data={
                    'type': 'batch_stalled',
                    'batch_id': str(batch.id),
                    'restaurant_ids': [str(r) for r in restaurant_ids],
                    'restaurant_count': len(restaurant_ids),
                },
            )
        except Exception as exc:
            logger.error(f"[BatchPipeline] Failed to alert on stalled batch: {exc}")


    @staticmethod
    def check_all_sections_ready():
        """
        Periodic check: find batches in FINALIZING/PRE_ASSIGNING where all
        sections are READY and transition them to PENDING_DRIVER.
        """
        from apps.orders.models import DeliveryBatch

        candidates = DeliveryBatch.objects.filter(
            status__in=['FINALIZING', 'PRE_ASSIGNING', 'PARTIAL_BATCH'],
        )
        for batch in candidates:
            BatchPipeline._check_all_sections_ready(batch)

    @staticmethod
    def check_and_expire_pending_driver():
        """
        Periodic check: expire batches stuck in PENDING_DRIVER past timeout.
        No driver was assigned within PENDING_DRIVER_TIMEOUT_MINUTES.
        Handles both new batches (with waiting_since set) and old batches
        (waiting_since is NULL — falls back to updated_at).
        """
        from apps.orders.models import DeliveryBatch
        from django.db.models import Q
        from domain.services.driver_free_service import DriverFreeService

        cutoff = timezone.now() - timezone.timedelta(minutes=PENDING_DRIVER_TIMEOUT_MINUTES)
        stale = DeliveryBatch.objects.filter(
            Q(status='PENDING_DRIVER'),
            Q(waiting_since__lt=cutoff) | Q(waiting_since__isnull=True, updated_at__lt=cutoff),
        )
        for batch in stale:
            try:
                DriverFreeService.release(
                    batch_id=str(batch.id),
                    event='TIMEOUT',
                    reason=f"No driver assigned within {PENDING_DRIVER_TIMEOUT_MINUTES} min"
                )
                logger.info(
                    f"[BatchPipeline] Expired batch {batch.id} — "
                    f"no driver assigned for >{PENDING_DRIVER_TIMEOUT_MINUTES} min"
                )
            except Exception as exc:
                logger.error(f"[BatchPipeline] Failed to expire batch {batch.id}: {exc}")

    # ─── Pricing helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _calculate_pricing(orders: list) -> dict:
        from domain.services.pricing_service import PricingService

        original_total = Decimal('0')
        per_order: dict = {}

        for order in orders:
            fee = Decimal(str(order.delivery_fee or 0))
            if fee == Decimal('0') and order.restaurant and hasattr(order, 'delivery_location') and order.delivery_location:
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
        # user_paid_total includes delivery fees + service fees.
        # We do NOT cap driver_payout against the items subtotal — drivers are paid
        # from the delivery/service fees collected, not from the food revenue.
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
            driver_payout = max(min_guarantee, total_fees * Decimal('0.95'))

        # Cap driver_payout to what was actually collected in delivery/service fees,
        # but always pay at least the minimum guarantee regardless of fee collected
        # (the platform absorbs shortfalls on very cheap or zero-fee orders).
        if user_paid_total > 0:
            driver_payout = min(driver_payout, user_paid_total)
        # Never pay negative
        driver_payout = max(driver_payout, Decimal('0'))

        if driver_payout > user_paid_total and user_paid_total > 0:
            logger.warning(
                f"[BatchPipeline] Driver payout ({driver_payout}) capped to "
                f"user_paid_total ({user_paid_total})"
            )
        return driver_payout, user_paid_total


# ─── Helpers ──────────────────────────────────────────────────────────────────

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
