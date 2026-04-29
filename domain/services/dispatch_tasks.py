from celery import shared_task
from domain.services.order_services import DispatchService
import logging

logger = logging.getLogger(__name__)

@shared_task(bind=True, max_retries=5)
def retry_dispatch_task(self, order_id, retry_count=0):
    """
    Background task to retry order dispatching.
    Retries up to 5 times with exponential backoff or custom delay.
    """
    logger.info(f"Retrying dispatch for Order {order_id}, attempt {retry_count + 1}")
    
    order = DispatchService.execute(order_id, is_retry=True)
    
    if not order.driver:
        if retry_count < self.max_retries:
            # Retry in 60 seconds
            self.retry(kwargs={'order_id': order_id, 'retry_count': retry_count + 1}, countdown=60)
        else:
            logger.error(f"Failed to dispatch Order {order_id} after {self.max_retries} attempts. Alerting admin.")
            # Trigger Admin Alert (already handled by DispatchFailed event if we add it)
