from django.db import models
import uuid

class IdempotencyKey(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=255, unique=True)
    user = models.ForeignKey('users.User', on_delete=models.CASCADE, null=True, blank=True)
    response_code = models.IntegerField()
    response_body = models.JSONField() # Store the response JSON
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['key', 'user']),
            models.Index(fields=['created_at']),
        ]

class Notification(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey('users.User', on_delete=models.CASCADE, related_name='notifications')
    title = models.CharField(max_length=255)
    body = models.TextField()
    data = models.JSONField(null=True, blank=True) # For extra payload like order_id
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_read']),
        ]

    def __str__(self):
        return f"{self.user.phone} - {self.title}"
