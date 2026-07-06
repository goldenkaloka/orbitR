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

    # Dispatch to each handler independently so one failing handler
    # does not block the others (e.g. a payment release failure must not
    # prevent the notification from being sent).
    import logging as _logging
    _logger = _logging.getLogger(__name__)
    last_exc = None
    for handler in EventBus._handlers[type(event)]:
        try:
            handler(event)
        except Exception as exc:
            _logger.exception(
                f"Handler {handler.__name__} failed for event "
                f"{event_log.event_type}/{event_log.aggregate_id}: {exc}"
            )
            last_exc = exc

    # Re-raise the last exception so Celery retries the whole task.
    # Handlers that already succeeded are idempotent (payment status guards,
    # notification deduplication, etc.) so re-running them is safe.
    if last_exc:
        raise last_exc

    event_log.status = 'PROCESSED'
    event_log.processed_at = timezone.now()
    event_log.save(update_fields=['status', 'processed_at'])
