from django.db import models
import uuid

class EventLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    aggregate_type = models.CharField(max_length=100) # e.g. 'Order', 'Payment'
    aggregate_id = models.UUIDField()
    event_type = models.CharField(max_length=100) # e.g. 'OrderCreated'
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=20, default='PENDING', db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['aggregate_type', 'aggregate_id', 'event_type']),
            models.Index(fields=['aggregate_id']),
        ]

    def __str__(self):
        return f"Event: {self.event_type} on {self.aggregate_type} ({self.aggregate_id})"
