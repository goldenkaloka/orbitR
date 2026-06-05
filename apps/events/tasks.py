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
