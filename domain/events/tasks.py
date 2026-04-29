from celery import shared_task
from apps.events.models import EventLog
from domain.events.bus import EventBus
import importlib

@shared_task
def dispatch_event_task(event_log_id):
    try:
        event_log = EventLog.objects.get(id=event_log_id)
        
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
    except Exception as e:
        import logging
        logging.error(f"Error in dispatch_event_task: {str(e)}")
