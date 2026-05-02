from collections import defaultdict

class EventBus:
    _handlers = defaultdict(list)

    @classmethod
    def subscribe(cls, event_type, handler):
        if handler not in cls._handlers[event_type]:
            cls._handlers[event_type].append(handler)

    @classmethod
    def publish(cls, event):
        from apps.events.models import EventLog
        from django.db import transaction
        
        # 1. Log the event immediately (within current transaction)
        event_log = EventLog.objects.create(
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=event.payload
        )
        
        # 2. Dispatch using Celery task ONLY after transaction commits
        from domain.events.tasks import dispatch_event_task
        transaction.on_commit(lambda: dispatch_event_task.delay(str(event_log.id)))

    @classmethod
    def _dispatch(cls, event):
        for handler in cls._handlers[type(event)]:
            handler(event)

