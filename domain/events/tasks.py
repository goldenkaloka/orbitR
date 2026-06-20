import importlib

from apps.events.models import EventLog
from celery import shared_task
from domain.events.bus import EventBus


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
    retry_jitter=True
)
def dispatch_event_task(self, event_log_id):
    from django.utils import timezone
    event_log = EventLog.objects.get(id=event_log_id)
    
    if event_log.status == 'PROCESSED':
        return
    
    types_module = importlib.import_module('domain.events.types')
    EventClass = getattr(types_module, event_log.event_type, None)
    
    if not EventClass:
        import logging
        logging.error(f"EventClass {event_log.event_type} not found")
        return
        
    event = EventClass(
        aggregate_id=event_log.aggregate_id,
        payload=event_log.payload
    )
    
    EventBus._dispatch(event)
    
    event_log.status = 'PROCESSED'
    event_log.processed_at = timezone.now()
    event_log.save(update_fields=['status', 'processed_at'])
