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


class BatchPipeline:
    """
    New order-time batching entry point.
    Call BatchPipeline.on_order_confirmed(order) when a vendor confirms an order.
    """

    @staticmethod
    def on_order_confirmed(order):
        """
        Called when an order is confirmed by the restaurant.
        Creates a new ACCUMULATING batch or joins an existing one.
        """
        from apps.orders.models import DeliveryBatch

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
        # force-join the oldest batch instead of creating a new one.
        # This prevents the kitchen from being overwhelmed by too many batches.
        existing_count = DeliveryBatch.objects.filter(
            restaurant_id=order.restaurant_id,
            status='ACCUMULATING',
        ).count()
        if existing_count >= MAX_ACCUMULATING_BATCHES_PER_RESTAURANT:
            oldest = (
                DeliveryBatch.objects.filter(
                    restaurant_id=order.restaurant_id,
                    status='ACCUMULATING',
                )
                .order_by('accumulating_since')
                .first()
            )
            if oldest:
                logger.warning(
                    f"[BatchPipeline] Restaurant {order.restaurant_id} at max batches "
                    f"({existing_count}) — force-joining order {order.id} to batch {oldest.id}"
                )
                return BatchPipeline._join_batch(order, oldest)

        # No compatible batch found — create a new ACCUMULATING batch
        return BatchPipeline._create_single_batch(order)

    @staticmethod
    def on_pool_ready(pool):
        """
        Create a dedicated batch for a pool. Pools always dispatch as a single unit.
        """
        return BatchPipeline._create_pool_batch(pool)

    @staticmethod
    def _find_compatible_batch(order):
        """
        Find an existing ACCUMULATING or SOFT_LOCK batch for the same restaurant
        that can accept this order.
        """
        from apps.orders.models import DeliveryBatch, Order

        restaurant_id = order.restaurant_id

        # Prefer ACCUMULATING first, then SOFT_LOCK
        accumulating = list(
            DeliveryBatch.objects.filter(
                restaurant_id=restaurant_id,
                status='ACCUMULATING',
            )
            .annotate(order_count=models.Count('orders'))
            .filter(order_count__lt=MAX_ORDERS_PER_BATCH)
            .order_by('accumulating_since')
        )

        for batch in accumulating:
            if BatchPipeline._is_compatible(order, batch):
                return batch

        soft_lock = list(
            DeliveryBatch.objects.filter(
                restaurant_id=restaurant_id,
                status='SOFT_LOCK',
            )
            .annotate(order_count=models.Count('orders'))
            .filter(order_count__lt=MAX_ORDERS_PER_BATCH)
            .order_by('soft_lock_at')
        )

        for batch in soft_lock:
            if BatchPipeline._is_compatible(order, batch, is_soft_lock=True):
                return batch

        return None

    @staticmethod
    def _is_compatible(order, batch, is_soft_lock=False):
        """
        Check if an order is compatible with a batch.
        Criteria: H3 delivery proximity via disk rings.
        ACCUMULATING: k=3 ring. SOFT_LOCK: k=1 ring (stricter).
        Also checks that the order isn't too distant from existing batch orders.
        """
        import h3
        from apps.orders.models import OrderItem

        first_order = batch.orders.first()
        if not first_order or not order.h3_index or not first_order.h3_index:
            return True

        ring_k = 1 if is_soft_lock else 3
        neighbor_cells = set(h3.grid_disk(first_order.h3_index, ring_k))
        if order.h3_index not in neighbor_cells:
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
            locked_batch.save(update_fields=[
                'last_order_confirmed_at',
                'user_paid_total', 'original_total_fee', 'discounted_total_fee',
            ])

            BatchPipeline._create_section_statuses_from_orders(locked_batch, [order])

        logger.info(
            f"[BatchPipeline] Order {order.id} joined batch {batch.id} "
            f"(status={locked_batch.status}, total_orders={locked_batch.orders.count()})"
        )

        # If batch reached max orders while ACCUMULATING, close it
        if locked_batch.status == 'ACCUMULATING' and locked_batch.orders.count() >= MAX_ORDERS_PER_BATCH:
            BatchPipeline._close_accumulating_batch(locked_batch.id)

        return locked_batch

    @staticmethod
    def _create_single_batch(order):
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
                accumulate_window_minutes=ACCUMULATE_WINDOW_MINUTES,
            )

            locked_order.batch = batch
            locked_order.route_sequence = 1
            locked_order.save(update_fields=['batch', 'route_sequence'])

            BatchPipeline._create_section_statuses_from_orders(batch, [locked_order])

        logger.info(
            f"[BatchPipeline] Created ACCUMULATING batch {batch.id} for order {order.id}"
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
                if batch_ref.status in ('FINALIZING', 'PRE_ASSIGNING', 'PARTIAL_BATCH'):
                    batch_ref.status = BatchFSM.transition(batch_ref, 'ALL_SECTIONS_READY')
                    batch_ref.waiting_since = timezone.now()
                    batch_ref.save(update_fields=['status', 'waiting_since', 'updated_at'])
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

            driver_share = policy.single_order_driver_share if policy else Decimal('0.90')
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
            accumulating_since__isnull=False,
            accumulating_since__lt=now - timezone.timedelta(minutes=ACCUMULATE_WINDOW_MINUTES),
        ).only('id')
        for batch in expired:
            BatchPipeline._close_accumulating_batch(str(batch.id))

        # ── Pass 1b: Busy restaurants — shorter window ──
        busy = (
            DeliveryBatch.objects.filter(status='ACCUMULATING')
            .values('restaurant_id')
            .annotate(cnt=Count('id'))
            .filter(cnt__gte=MAX_ACCUMULATING_BATCHES_PER_RESTAURANT)
        )
        busy_restaurant_ids = [b['restaurant_id'] for b in busy]
        if busy_restaurant_ids:
            busy_expired = DeliveryBatch.objects.filter(
                restaurant_id__in=busy_restaurant_ids,
                status='ACCUMULATING',
                accumulating_since__isnull=False,
                accumulating_since__lt=now - timezone.timedelta(minutes=BUSY_ACCUMULATE_WINDOW_MINUTES),
            ).only('id')
            for batch in busy_expired:
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
                with transaction.atomic():
                    batch = DeliveryBatch.objects.select_for_update().get(id=bss.batch_id)
                    if batch.status in ('FINALIZING', 'SOFT_LOCK', 'PARTIAL_BATCH'):
                        batch.status = BatchFSM.transition(batch, 'SECTION_FAIL')
                        batch.save(update_fields=['status', 'updated_at'])
                    logger.warning(
                        f"[BatchPipeline] Section {bss.section_id} in batch {bss.batch_id} "
                        f"stuck for >{SECTION_TIMEOUT_MINUTES}min — batch → PARTIAL_BATCH"
                    )
            except Exception as exc:
                logger.error(f"[BatchPipeline] Section timeout escalation failed: {exc}")


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
