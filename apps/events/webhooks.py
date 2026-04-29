import requests
import logging
from django.conf import settings
from celery import shared_task

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=3)
def fire_order_webhook(self, event_type: str, payload: dict):
    """
    Fires an HTTP POST webhook to external systems configured by the platform.
    In a real-world scenario, merchants would have WebhookEndpoint models.
    """
    webhook_url = getattr(settings, 'ORDER_WEBHOOK_URL', None)
    
    if not webhook_url:
        return # Webhooks not configured
        
    data = {
        'event': event_type,
        'data': payload
    }
    
    try:
        response = requests.post(
            webhook_url,
            json=data,
            headers={'Content-Type': 'application/json'},
            timeout=5
        )
        response.raise_for_status()
        logger.info(f"Webhook delivered for {event_type} to {webhook_url}")
    except requests.exceptions.RequestException as exc:
        logger.warning(f"Webhook delivery failed for {event_type}: {exc}")
        # Retry with exponential backoff (e.g. 5, 25, 125 seconds)
        raise self.retry(exc=exc, countdown=5 ** self.request.retries)
