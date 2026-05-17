from django.db import transaction
from django.utils import timezone
from django.core.cache import cache
from django.core.management import call_command
from decimal import Decimal
import logging

from domain.events.bus import EventBus
from domain.events.types import BatchOffered, BatchAccepted, BatchDeclined, BatchExpired

logger = logging.getLogger(__name__)

MAX_OFFER_ATTEMPTS = 5
OFFER_EXPIRY_SECONDS = 300  # 5 minutes
OFFER_CACHE_PREFIX = "batch_offer_"


class BatchDispatchService:
    """
    Unified batch dispatch service.
    Every order becomes a batch (single or multi-order).
    Uses EDA pattern - publishes events for each state change.
    """

    @staticmethod
    def dispatch_batch(batch_id):
        """
        Main entry point to dispatch a batch to a driver.
        Called when batch is created or when retry needed.
        """
        from apps.orders.models import DeliveryBatch

        try:
            batch = DeliveryBatch.objects.select_related('restaurant').get(id=batch_id)
        except DeliveryBatch.DoesNotExist:
            logger.error(f"Batch {batch_id} not found")
            return None

        if batch.status != 'CREATED':
            logger.warning(f"Batch {batch_id} is in {batch.status} status, cannot dispatch")
            return None

        offer_attempt = batch.offer_attempt if hasattr(batch, 'offer_attempt') else 0

        if offer_attempt >= MAX_OFFER_ATTEMPTS:
            logger.warning(f"Batch {batch_id} reached max offer attempts ({MAX_OFFER_ATTEMPTS})")
            batch.status = 'CREATED'
            batch.save()
            EventBus.publish(BatchExpired(
                aggregate_id=str(batch.id),
                payload={'reason': 'max_attempts_reached'}
            ))
            return None

        lock_key = f"dispatch_batch_{batch.id}"
        if not cache.add(lock_key, "1", 60):
            logger.debug(f"Batch {batch.id} already being dispatched")
            return None

        try:
            driver = BatchDispatchService._find_best_driver(batch)
            if not driver:
                logger.warning(f"No driver found for batch {batch.id}")
                BatchDispatchService._schedule_retry(batch.id, attempt=offer_attempt + 1)
                return None

            BatchDispatchService._offer_to_driver(batch, driver)
            return batch

        finally:
            cache.delete(lock_key)

    @staticmethod
    def _find_best_driver(batch):
        """Use CrossBatchDriverMatcher to find best driver for batch"""
        from domain.services.cross_batching_service import CrossBatchDriverMatcher

        return CrossBatchDriverMatcher.find_best_driver_for_batch(batch, max_radius_km=15)

    @staticmethod
    def _offer_to_driver(batch, driver):
        """Offer batch to driver"""
        from apps.delivery.models import Driver
        from domain.fsm.driver_fsm import DriverFSM
        from apps.events.utils import broadcast_order_status

        with transaction.atomic():
            batch = DeliveryBatch.objects.select_for_update().get(id=batch.id)
            driver = Driver.objects.select_for_update().get(id=driver.id)

            order_count = batch.orders.count()

            batch.driver = driver
            batch.status = 'OFFERED'
            batch.offer_attempt = getattr(batch, 'offer_attempt', 0) + 1
            batch.offer_sent_at = timezone.now()
            batch.save()

            driver.current_active_orders += order_count
            driver.status = DriverFSM.transition(driver.status, 'ASSIGNED')
            driver.save()

            for order in batch.orders.select_related('restaurant').all():
                order.status = 'OFFERED'
                order.driver = driver
                order.save()

            cache.set(
                f"{OFFER_CACHE_PREFIX}{batch.id}",
                {'driver_id': str(driver.id), 'offered_at': str(timezone.now())},
                OFFER_EXPIRY_SECONDS
            )

            EventBus.publish(BatchOffered(
                aggregate_id=str(batch.id),
                payload={
                    'driver_id': str(driver.id),
                    'batch_id': str(batch.id),
                    'num_orders': order_count,
                    'payout': float(batch.driver_payout),
                    'attempt': batch.offer_attempt
                }
            ))

            batch_id_str = str(batch.id)
            transaction.on_commit(lambda: broadcast_order_status(
                batch_id_str,
                'OFFERED',
                f"You have {batch.orders.count()} order(s) to deliver. Payout: TSh {batch.driver_payout}"
            ))

            logger.info(f"Batch {batch.id} offered to driver {driver.id}")

    @staticmethod
    def _schedule_retry(batch_id, attempt):
        """Schedule retry after short delay"""
        from django.utils import timezone
        from datetime import timedelta

        batch = DeliveryBatch.objects.get(id=batch_id)
        batch.offer_attempt = attempt
        batch.status = 'CREATED'
        batch.save()

        retry_at = timezone.now() + timedelta(seconds=30)
        logger.info(f"Scheduling retry for batch {batch_id} at {retry_at}")

    @staticmethod
    def accept_batch(batch_id, driver_id, actor):
        """Driver accepts batch offer"""
        from apps.orders.models import DeliveryBatch
        from apps.delivery.models import DeliveryAssignment

        with transaction.atomic():
            batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
            if batch.status != 'OFFERED':
                raise ValueError(f"Batch is not in OFFERED status: {batch.status}")

            if str(batch.driver_id) != str(driver_id):
                raise ValueError("This batch was not offered to you")

            batch.status = 'ASSIGNED'
            batch.save()

            for order in batch.orders.all():
                order.status = 'ASSIGNED'
                order.save()
                DeliveryAssignment.objects.update_or_create(
                    order=order,
                    defaults={'driver_id': driver_id, 'batch': batch}
                )

            cache.delete(f"{OFFER_CACHE_PREFIX}{batch_id}")

            EventBus.publish(BatchAccepted(
                aggregate_id=str(batch.id),
                payload={'driver_id': str(driver_id), 'actor_id': str(actor.id)}
            ))

            logger.info(f"Driver {driver_id} accepted batch {batch_id}")
            return batch

    @staticmethod
    def decline_batch(batch_id, driver_id, actor):
        """Driver declines batch offer - auto re-offer to next driver"""
        from apps.orders.models import DeliveryBatch

        with transaction.atomic():
            batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)

            if batch.status != 'OFFERED':
                raise ValueError(f"Batch is not in OFFERED status: {batch.status}")

            if str(batch.driver_id) != str(driver_id):
                raise ValueError("This batch was not offered to you")

            batch.driver = None
            batch.status = 'CREATED'

            order_count = batch.orders.count()
            
            for order in batch.orders.all():
                order.status = 'READY'
                order.driver = None
                order.save()

            if batch.driver:
                batch.driver.current_active_orders = max(0, batch.driver.current_active_orders - order_count)
                from domain.fsm.driver_fsm import DriverFSM
                try:
                    new_status = DriverFSM.transition(batch.driver.status, 'AVAILABLE')
                    batch.driver.status = new_status
                except Exception:
                    pass
                batch.driver.save()

            cache.delete(f"{OFFER_CACHE_PREFIX}{batch_id}")

            EventBus.publish(BatchDeclined(
                aggregate_id=str(batch.id),
                payload={'driver_id': str(driver_id), 'actor_id': str(actor.id)}
            ))

            logger.info(f"Driver {driver_id} declined batch {batch_id}")
            
            if batch.offer_attempt >= MAX_OFFER_ATTEMPTS:
                logger.warning(f"Batch {batch_id} reached max offer attempts ({MAX_OFFER_ATTEMPTS}), marking as expired")
                batch.status = 'EXPIRED'
                batch.save()
                
                EventBus.publish(BatchExpired(
                    aggregate_id=str(batch.id),
                    payload={'reason': 'max_attempts_reached'}
                ))
                return batch

        BatchDispatchService.dispatch_batch(batch_id)
        return batch


class BatchOfferExpiryHandler:
    """
    Handles batch offer expiry.
    When offer expires, automatically re-offer to next best driver.
    """

    @staticmethod
    def handle_expiry(batch_id):
        """Called when batch offer expires (5 minutes)"""
        from apps.orders.models import DeliveryBatch

        try:
            batch = DeliveryBatch.objects.get(id=batch_id)

            if batch.status != 'OFFERED':
                logger.info(f"Batch {batch_id} is in {batch.status} status, ignoring expiry")
                return

            offer_data = cache.get(f"{OFFER_CACHE_PREFIX}{batch_id}")
            if offer_data:
                cache.delete(f"{OFFER_CACHE_PREFIX}{batch_id}")

            batch.driver = None
            batch.status = 'CREATED'
            
            order_count = batch.orders.count()
            
            driver = batch.driver
            if driver:
                driver.current_active_orders = max(0, driver.current_active_orders - order_count)
                from domain.fsm.driver_fsm import DriverFSM
                try:
                    new_status = DriverFSM.transition(driver.status, 'AVAILABLE')
                    driver.status = new_status
                except Exception:
                    pass
                driver.save()
            
            batch.save()

            for order in batch.orders.all():
                order.status = 'READY'
                order.driver = None
                order.save()

            EventBus.publish(BatchExpired(
                aggregate_id=str(batch.id),
                payload={'reason': 'offer_timeout'}
            ))

            logger.info(f"Batch {batch_id} offer expired")
            
            if batch.offer_attempt >= MAX_OFFER_ATTEMPTS:
                logger.warning(f"Batch {batch_id} reached max offer attempts ({MAX_OFFER_ATTEMPTS}), marking as expired")
                batch.status = 'EXPIRED'
                batch.save()
                
                EventBus.publish(BatchExpired(
                    aggregate_id=str(batch.id),
                    payload={'reason': 'max_attempts_reached'}
                ))
                return

            BatchDispatchService.dispatch_batch(batch_id)

        except Exception as e:
            logger.error(f"Error handling batch offer expiry: {str(e)}")