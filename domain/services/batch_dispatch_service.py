"""
BatchDispatchService — Driver dispatch, accept, decline, and expiry handling.

Fixed bugs from original implementation:
  #1  Driver never freed after offer expiry  — now uses DriverFreeService.release()
  #2  Driver never freed after decline       — now uses DriverFreeService.release()
  #16 select_related('batch') on DeliveryBatch (no such field) — removed
  #17 No customer notification on delay      — added _notify_all_parties_on_delay()

All batch status transitions go through BatchFSM.
All driver releases go through DriverFreeService.
"""
import logging
from decimal import Decimal

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from domain.events.bus import EventBus
from domain.events.types import BatchOffered, BatchAccepted, BatchDeclined, BatchExpired
from domain.services.batch_fsm import BatchFSM, BatchFSMError
from domain.services.driver_free_service import DriverFreeService

logger = logging.getLogger(__name__)

MAX_OFFER_ATTEMPTS = 5
OFFER_EXPIRY_SECONDS = 300   # 5 minutes per offer
OFFER_CACHE_PREFIX = "batch_offer_"


# ─── Public helpers ───────────────────────────────────────────────────────────

def get_available_driver_count() -> int:
    """Returns count of drivers that are AVAILABLE and have a recent heartbeat."""
    from datetime import timedelta
    from apps.delivery.models import Driver
    threshold = timezone.now() - timedelta(minutes=5)
    return Driver.objects.filter(
        status='AVAILABLE',
        last_heartbeat__gte=threshold,
    ).count()


def notify_no_drivers_available(batch_id, attempt_count: int):
    """Alert admin when no drivers are found after multiple attempts."""
    from django.conf import settings
    from infrastructure.notifications.service import NotificationService

    logger.warning(f"[Dispatch] No drivers for batch {batch_id} (attempt {attempt_count})")
    key = f"no_drivers_alert_{batch_id}"
    if cache.get(key):
        return
    cache.set(key, True, timeout=1800)

    admin_uid = getattr(settings, 'ADMIN_NOTIFICATION_USER_ID', None)
    if admin_uid:
        try:
            NotificationService.send_push(
                user_id=str(admin_uid),
                title="No Drivers Available!",
                body=(
                    f"Batch {batch_id} cannot find drivers after "
                    f"{attempt_count} attempts. Manual intervention required."
                ),
                data={"batch_id": str(batch_id), "type": "no_drivers_alert"},
            )
        except Exception as exc:
            logger.error(f"[Dispatch] Failed to send no-drivers alert: {exc}")


def handle_expired_batch(batch_id: str):
    """
    Move batch to WAITING_TEMP_DRIVER and notify restaurant + customers.
    Called when MAX_OFFER_ATTEMPTS is reached.
    """
    from apps.orders.models import DeliveryBatch

    try:
        with transaction.atomic():
            # Fix #3 & #4: Lock batch inside transaction and use BatchFSM
            batch = DeliveryBatch.objects.select_for_update().prefetch_related(
                'orders__restaurant__owner',
                'orders__customer',
            ).get(id=batch_id)

            if batch.status != 'EXPIRED':
                # Already processed or transitioned
                return list(batch.orders.all())

            # Transition via BatchFSM: EXPIRED -> WAITING_TEMP_DRIVER
            batch.status = BatchFSM.transition(batch, 'ESCALATE')
            batch.waiting_since = timezone.now()
            batch.save(update_fields=['status', 'waiting_since'])

        _notify_all_parties_on_delay(batch)

        logger.warning(
            f"[Dispatch] Batch {batch_id} → WAITING_TEMP_DRIVER. "
            f"Orders: {batch.orders.count()}"
        )
        return list(batch.orders.all())

    except Exception as exc:
        logger.error(f"[Dispatch] Error handling expired batch {batch_id}: {exc}", exc_info=True)
        return None


def notify_restaurant_pending_batch(batch_id, restaurant_id, batch=None):
    """Notify a restaurant owner that their orders need a temp driver."""
    from apps.orders.models import DeliveryBatch
    from apps.restaurants.models import Restaurant
    from infrastructure.notifications.service import NotificationService

    try:
        if batch is None:
            batch = DeliveryBatch.objects.prefetch_related('orders').get(id=batch_id)

        restaurant = Restaurant.objects.select_related('owner').get(id=restaurant_id)
        if not restaurant.owner:
            return

        order_count = batch.orders.filter(restaurant_id=restaurant_id).count()
        if order_count == 0:
            return

        key = f"batch_notify_restaurant_{batch_id}_{restaurant_id}"
        if cache.get(key):
            return
        cache.set(key, True, timeout=1800)

        NotificationService.send_push(
            user_id=str(restaurant.owner.id),
            title="Need Driver! 🚗",
            body=(
                f"You have {order_count} order(s) waiting for delivery. "
                "Register a temp driver to deliver them!"
            ),
            data={
                "batch_id": str(batch_id),
                "restaurant_id": str(restaurant_id),
                "type": "batch_needs_driver",
            },
        )
    except Exception as exc:
        logger.error(f"[Dispatch] Failed to notify restaurant {restaurant_id}: {exc}")


# ─── Main service ─────────────────────────────────────────────────────────────

class BatchDispatchService:
    """
    Unified batch dispatch service.
    Every order becomes a batch (single or multi-order).
    Uses EDA pattern — publishes events for each state change.
    """

    @staticmethod
    def dispatch_batch(batch_id: str):
        """
        Main entry point to dispatch a batch to a driver.
        Called after batch creation (via on_commit) or on retry.
        """
        from apps.orders.models import DeliveryBatch

        try:
            batch = DeliveryBatch.objects.select_related('restaurant').get(id=batch_id)
        except DeliveryBatch.DoesNotExist:
            logger.error(f"[Dispatch] Batch {batch_id} not found")
            return None

        if not BatchFSM.can_dispatch(batch):
            logger.warning(
                f"[Dispatch] Batch {batch_id} is in status '{batch.status}' — "
                f"cannot dispatch (expected CREATED)"
            )
            return None

        if batch.offer_attempt >= MAX_OFFER_ATTEMPTS:
            logger.warning(
                f"[Dispatch] Batch {batch_id} reached max offer attempts "
                f"({MAX_OFFER_ATTEMPTS}) — escalating to EXPIRED"
            )
            try:
                batch.status = BatchFSM.transition(batch, 'MAX_ATTEMPTS')
                batch.save(update_fields=['status'])
            except BatchFSMError:
                batch.status = 'EXPIRED'
                batch.save(update_fields=['status'])

            EventBus.publish(BatchExpired(
                aggregate_id=str(batch.id),
                payload={'reason': 'max_attempts_reached'},
            ))
            handle_expired_batch(batch_id)
            return None

        # Concurrency guard
        lock_key = f"dispatch_batch_{batch.id}"
        if not cache.add(lock_key, "1", 60):
            logger.debug(f"[Dispatch] Batch {batch.id} already being dispatched")
            return None

        try:
            driver = BatchDispatchService._find_best_driver(batch)
            if not driver:
                available = get_available_driver_count()
                logger.warning(
                    f"[Dispatch] No driver for batch {batch.id}. "
                    f"System available drivers: {available}"
                )
                if available == 0:
                    notify_no_drivers_available(batch.id, batch.offer_attempt + 1)

                # Fix #6: Celery task handles retry via self.retry(), so we do not
                # schedule a duplicate retry task from here.
                return None

            BatchDispatchService._offer_to_driver(batch, driver)
            return batch

        finally:
            cache.delete(lock_key)

    @staticmethod
    def _find_best_driver(batch):
        from domain.services.cross_batching_service import CrossBatchDriverMatcher
        return CrossBatchDriverMatcher.find_best_driver_for_batch(batch, max_radius_km=15)

    @staticmethod
    def _offer_to_driver(batch, driver):
        from apps.delivery.models import Driver
        from apps.events.utils import broadcast_order_status
        from domain.fsm.driver_fsm import DriverFSM

        with transaction.atomic():
            batch = _lock_batch(batch.id)
            driver = Driver.objects.select_for_update().get(id=driver.id)

            order_count = batch.orders.count()

            if driver.current_active_orders + order_count > driver.max_active_orders:
                logger.warning(
                    f"[Dispatch] Driver {driver.id} would exceed capacity "
                    f"({driver.current_active_orders} + {order_count} > {driver.max_active_orders}). "
                    f"Aborting offer. Batch {batch.id} will be retried."
                )
                return False

            new_batch_status = BatchFSM.transition(batch, 'OFFER')
            batch.driver = driver
            batch.status = new_batch_status
            batch.offer_attempt = batch.offer_attempt + 1
            batch.offer_sent_at = timezone.now()
            batch.save(update_fields=['driver', 'status', 'offer_attempt', 'offer_sent_at'])

            # Driver is offered, not yet assigned — keep AVAILABLE so the driver
            # can still be found by other queries, but track the pending offer.
            # Orders stay READY; they are never set to OFFERED.
            # total_claim_attempts is incremented here to track offer volume.
            driver.total_claim_attempts = driver.total_claim_attempts + 1
            driver.save(update_fields=['total_claim_attempts'])

            cache.set(
                f"{OFFER_CACHE_PREFIX}{batch.id}",
                {'driver_id': str(driver.id), 'offered_at': str(timezone.now())},
                OFFER_EXPIRY_SECONDS,
            )

            payout_val = batch.driver_payout
            EventBus.publish(BatchOffered(
                aggregate_id=str(batch.id),
                payload={
                    'driver_id': str(driver.id),
                    'batch_id': str(batch.id),
                    'num_orders': order_count,
                    'payout': float(payout_val),
                    'attempt': batch.offer_attempt,
                },
            ))

            # Notify driver via push (order.driver is not set during offer window)
            from infrastructure.notifications.service import NotificationService
            try:
                NotificationService.send_push(
                    user_id=str(driver.user_id),
                    title="New Delivery Offer!",
                    body=f"{order_count} order(s) ready. Payout: TSh {payout_val}",
                    data={
                        'type': 'batch_offer',
                        'batch_id': str(batch.id),
                        'num_orders': order_count,
                        'payout': float(payout_val),
                    }
                )
            except Exception as exc:
                logger.warning(f"[Dispatch] Failed to push offer notification to driver {driver.id}: {exc}")

            # Broadcast the status to each order's channel for the tracking UI
            order_id_strs = [str(order.id) for order in batch.orders.all()]
            for oid in order_id_strs:
                transaction.on_commit(lambda oid=oid: broadcast_order_status(
                    oid, 'OFFERED',
                    f"You have {order_count} order(s) to deliver. "
                    f"Payout: TSh {payout_val}",
                ))

            logger.info(f"[Dispatch] Batch {batch.id} offered to driver {driver.id}")

    @staticmethod
    def _schedule_retry(batch_id: str, attempt: int):
        from domain.services.dispatch_tasks import retry_batch_dispatch_task
        from apps.orders.models import DeliveryBatch

        batch = DeliveryBatch.objects.get(id=batch_id)
        batch.offer_attempt = attempt
        batch.save(update_fields=['offer_attempt'])

        delays = {1: 30, 2: 60, 3: 120, 4: 300, 5: 600}
        delay = delays.get(attempt, 30)
        retry_batch_dispatch_task.apply_async(args=[str(batch_id), attempt], countdown=delay)
        logger.info(f"[Dispatch] Retry {attempt} scheduled for batch {batch_id} in {delay}s")

    # ─── Driver responses ─────────────────────────────────────────────────────

    @staticmethod
    def accept_batch(batch_id: str, driver_id: str, actor):
        """Driver accepts a batch offer. Re-dispatches any other pending offers to this driver."""
        from apps.delivery.models import Driver, DeliveryAssignment
        from apps.events.utils import broadcast_order_status
        from apps.orders.models import DeliveryBatch, Order
        from domain.fsm.driver_fsm import DriverFSM

        with transaction.atomic():
            batch = _lock_batch(batch_id)

            if batch.status not in ('OFFERED', 'WAITING_TEMP_DRIVER'):
                raise ValueError(f"Batch is in {batch.status} state — cannot accept.")
            if str(batch.driver_id) != str(driver_id):
                raise ValueError("This batch was not offered to you")

            driver = Driver.objects.select_for_update().get(id=driver_id)

            # Prevent driver from accepting when they already have active orders.
            from apps.orders.models import Order
            active_orders = Order.objects.filter(
                driver_id=driver_id, status__in=['ASSIGNED', 'PICKED']
            ).exclude(batch_id=batch_id).exists()
            if active_orders:
                raise ValueError("Driver already has active deliveries in progress")

            order_count = batch.orders.count()

            batch.status = BatchFSM.transition(batch, 'ACCEPT')
            batch.save(update_fields=['status'])

            orders = list(batch.orders.all())
            for order in orders:
                order.status = 'ASSIGNED'
            Order.objects.bulk_update(orders, ['status'])

            # Driver transitions directly from AVAILABLE → DELIVERING.
            # The ASSIGNED driver state has been removed.
            driver.status = DriverFSM.transition(driver.status, 'DELIVERING')
            driver.current_active_orders += order_count
            driver.total_claims_accepted = driver.total_claims_accepted + 1
            driver.save(update_fields=['status', 'current_active_orders', 'total_claims_accepted'])

            assignments = [
                DeliveryAssignment(order_id=o.id, driver_id=driver_id)
                for o in orders
            ]
            DeliveryAssignment.objects.bulk_create(assignments, ignore_conflicts=True)

            cache.delete(f"{OFFER_CACHE_PREFIX}{batch_id}")

            EventBus.publish(BatchAccepted(
                aggregate_id=str(batch.id),
                payload={'driver_id': str(driver_id), 'actor_id': str(actor.id)},
            ))

            logger.info(f"[Dispatch] Driver {driver_id} accepted batch {batch_id}")

            # Re-dispatch any other batches that were offered to this driver
            # while they were considering this one. Must be inside on_commit so
            # the accept transaction is committed before we try to re-offer.
            def _reassign_other_batches():
                from domain.services.dispatch_tasks import dispatch_batch_task
                from apps.orders.models import DeliveryBatch
                other_offers = DeliveryBatch.objects.filter(
                    driver_id=driver_id,
                    status='OFFERED'
                ).exclude(id=batch_id)

                for other_batch in other_offers:
                    logger.info(
                        f"[Dispatch] Re-dispatching batch {other_batch.id} to another driver "
                        f"(driver {driver_id} already accepted {batch_id})"
                    )
                    DriverFreeService.release(
                        str(other_batch.id), event='DECLINE',
                        reason='Driver accepted another batch'
                    )
                    dispatch_batch_task.delay(str(other_batch.id))

            transaction.on_commit(_reassign_other_batches)

        return batch

    @staticmethod
    def decline_batch(batch_id: str, driver_id: str, actor):
        """
        Driver declines a batch offer.
        Fix #2: DriverFreeService.release() properly handles driver state.
        Fix #3: Lock batch in decline_batch before validations.
        """
        from apps.orders.models import DeliveryBatch

        with transaction.atomic():
            batch = _lock_batch(batch_id)

            if batch.status != 'OFFERED':
                raise ValueError(f"Batch is not in OFFERED status: {batch.status}")
            if str(batch.driver_id) != str(driver_id):
                raise ValueError("This batch was not offered to you")

            cache.delete(f"{OFFER_CACHE_PREFIX}{batch_id}")

            EventBus.publish(BatchDeclined(
                aggregate_id=str(batch.id),
                payload={'driver_id': str(driver_id), 'actor_id': str(actor.id)},
            ))

            # DriverFreeService atomically releases driver, resets orders, transitions batch
            DriverFreeService.release(batch_id, event='DECLINE', reason=f"Declined by driver {driver_id}")

            # Re-fetch to check updated attempt count
            batch.refresh_from_db()

            if batch.offer_attempt >= MAX_OFFER_ATTEMPTS:
                logger.warning(f"[Dispatch] Batch {batch_id} reached max attempts → escalating")
                batch.status = BatchFSM.transition(batch, 'MAX_ATTEMPTS')
                batch.save(update_fields=['status'])
                EventBus.publish(BatchExpired(
                    aggregate_id=str(batch.id),
                    payload={'reason': 'max_attempts_reached'},
                ))
                transaction.on_commit(lambda: handle_expired_batch(batch_id))
                return batch

            # Trigger Celery task asynchronously instead of synchronous recursive call
            from domain.services.dispatch_tasks import dispatch_batch_task
            transaction.on_commit(lambda: dispatch_batch_task.delay(batch_id))
            return batch


# ─── Offer expiry handler ─────────────────────────────────────────────────────

class BatchOfferExpiryHandler:
    """
    Handles batch offer timeout (5-minute window expired).
    Fix #1: Uses DriverFreeService.release() instead of the broken manual pattern.
    """

    @staticmethod
    def handle_expiry(batch_id: str):
        from apps.orders.models import DeliveryBatch

        try:
            with transaction.atomic():
                batch = _lock_batch(batch_id)

                if batch.status != 'OFFERED':
                    logger.info(
                        f"[ExpiryHandler] Batch {batch_id} is in '{batch.status}' — ignoring expiry"
                    )
                    return

                cache.delete(f"{OFFER_CACHE_PREFIX}{batch_id}")

                EventBus.publish(BatchExpired(
                    aggregate_id=str(batch.id),
                    payload={'reason': 'offer_timeout'},
                ))

                # DriverFreeService atomically releases driver, resets orders, transitions batch
                DriverFreeService.release(batch_id, event='TIMEOUT', reason='Offer window expired')

                batch.refresh_from_db()

                if batch.offer_attempt >= MAX_OFFER_ATTEMPTS:
                    logger.warning(f"[ExpiryHandler] Batch {batch_id} reached max attempts")
                    batch.status = BatchFSM.transition(batch, 'MAX_ATTEMPTS')
                    batch.save(update_fields=['status'])
                    EventBus.publish(BatchExpired(
                        aggregate_id=str(batch.id),
                        payload={'reason': 'max_attempts_reached'},
                    ))
                    transaction.on_commit(lambda: handle_expired_batch(batch_id))
                    return

                from domain.services.dispatch_tasks import dispatch_batch_task
                transaction.on_commit(lambda: dispatch_batch_task.delay(batch_id))

        except Exception as exc:
            logger.error(f"[ExpiryHandler] Error for batch {batch_id}: {exc}", exc_info=True)


# ─── Private helpers ──────────────────────────────────────────────────────────

def _lock_batch(batch_id):
    """Fetch a DeliveryBatch with a row-level lock. Fix #9 (PascalCase)."""
    from apps.orders.models import DeliveryBatch
    return DeliveryBatch.objects.select_for_update().get(id=batch_id)


def _notify_all_parties_on_delay(batch):
    """
    Fix #17: Notify BOTH restaurant owners AND customers when no driver is found.
    Original code only notified restaurants.
    """
    from apps.events.utils import broadcast_order_status

    restaurants_seen = set()
    for order in batch.orders.select_related('customer', 'restaurant').all():
        # Notify customer
        try:
            from infrastructure.notifications.service import NotificationService
            NotificationService.send_push(
                user_id=str(order.customer.id),
                title="Delivery Delayed ⏳",
                body=(
                    "We're finding a driver for your order. "
                    "You'll be notified once one is assigned."
                ),
                data={"order_id": str(order.id), "type": "delivery_delayed"},
            )
            broadcast_order_status(
                str(order.id),
                'WAITING_DRIVER',
                "Finding a driver for your order...",
            )
        except Exception as exc:
            logger.warning(f"[Dispatch] Failed to notify customer for order {order.id}: {exc}")

        # Notify restaurant owner (once per restaurant)
        if order.restaurant_id not in restaurants_seen:
            restaurants_seen.add(order.restaurant_id)
            notify_restaurant_pending_batch(str(batch.id), order.restaurant_id, batch)