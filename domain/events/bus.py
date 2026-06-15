from collections import defaultdict
from django.db import transaction


class EventBus:
    _handlers = defaultdict(list)

    @classmethod
    def subscribe(cls, event_type, handler):
        if handler not in cls._handlers[event_type]:
            cls._handlers[event_type].append(handler)

    @classmethod
    def publish(cls, event):
        from apps.events.models import EventLog
        from domain.events.tasks import dispatch_event_task

        event_log = EventLog.objects.create(
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            event_type=event.event_type,
            payload=event.payload,
            status='PENDING'
        )

        task_id = str(event_log.id)

        if transaction.get_connection().in_atomic_block:
            transaction.on_commit(lambda: dispatch_event_task.delay(task_id))
        else:
            dispatch_event_task.delay(task_id)

    @classmethod
    def _dispatch(cls, event):
        for handler in cls._handlers[type(event)]:
            handler(event)

