from celery import shared_task
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
from apps.delivery.models import Driver
from domain.fsm.driver_fsm import DriverFSM
import logging

logger = logging.getLogger(__name__)

STALE_THRESHOLD_MINUTES = 5

@shared_task
def check_stale_drivers():
    """
    Marks drivers as OFFLINE if they haven't sent a heartbeat in 5 minutes.
    This prevents phantom job dispatch to unreachable drivers.
    """
    threshold = timezone.now() - timedelta(minutes=STALE_THRESHOLD_MINUTES)

    stale_drivers = Driver.objects.filter(
        Q(status='AVAILABLE') | Q(status='DELIVERING'),
        Q(last_heartbeat__isnull=True) | Q(last_heartbeat__lt=threshold)
    )

    count = 0
    for driver in stale_drivers:
        try:
            driver.status = DriverFSM.transition(driver.status, 'OFFLINE')
            driver.save(update_fields=['status'])
            logger.info(f"Driver {driver.id} marked as OFFLINE (stale heartbeat)")
            count += 1
        except Exception as e:
            logger.error(f"Failed to mark driver {driver.id} as OFFLINE: {e}")

    return f"Marked {count} stale drivers as OFFLINE"


@shared_task
def clear_stale_heartbeats():
    """
    Clears stale last_heartbeat values for drivers that are OFFLINE.
    """
    Driver.objects.filter(status='OFFLINE').update(last_heartbeat=None)
    return "Cleared stale heartbeats for offline drivers"