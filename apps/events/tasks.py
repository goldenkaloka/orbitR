import logging
from celery import shared_task
from .centrifugo import publish as _publish, broadcast as _broadcast

logger = logging.getLogger(__name__)

@shared_task(acks_late=True, max_retries=2, default_retry_delay=2)
def centrifugo_publish(channel: str, data: dict) -> bool:
    return _publish(channel, data)

@shared_task(acks_late=True, max_retries=2, default_retry_delay=2)
def centrifugo_broadcast(channels: list, data: dict) -> bool:
    return _broadcast(channels, data)

@shared_task(name='apps.events.tasks.retry_stuck_events')
def retry_stuck_events():
    """
    Find event logs stuck in PENDING status for > 2 minutes and retry dispatch.
    """
    from apps.events.models import EventLog
    from domain.events.tasks import dispatch_event_task
    from django.utils import timezone
    from datetime import timedelta

    threshold = timezone.now() - timedelta(minutes=2)
    stuck_events = EventLog.objects.filter(status='PENDING', created_at__lt=threshold)

    retried = 0
    for event in stuck_events:
        dispatch_event_task.delay(str(event.id))
        retried += 1

    if retried:
        logger.info(f"Retried {retried} stuck EventLog dispatches.")
