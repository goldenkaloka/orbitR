from ..models import Event


def emit(event_type, entity_id, payload=None):
    Event.objects.create(
        event_type=event_type,
        entity_id=entity_id,
        payload=payload or {}
    )

