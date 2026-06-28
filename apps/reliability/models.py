import uuid
from django.db import models
from django.utils import timezone


class ReliabilityRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey('users.User', on_delete=models.CASCADE, related_name='reliability_records', db_index=True)
    role = models.CharField(max_length=20, choices=[
        ('customer', 'Customer'),
        ('vendor', 'Vendor'),
        ('driver', 'Driver'),
    ], db_index=True)
    score = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'role')
        verbose_name_plural = 'reliability records'

    def __str__(self):
        return f"{self.user.username} ({self.role}) — score {self.score}"


class Strike(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.ForeignKey(ReliabilityRecord, on_delete=models.CASCADE, related_name='strikes')
    reason = models.CharField(max_length=50, choices=[
        ('cancel_created', 'Cancelled at CREATED'),
        ('cancel_confirmed', 'Cancelled at CONFIRMED'),
        ('cancel_preparing', 'Cancelled during PREPARING'),
        ('cancel_assigned', 'Cancelled after ASSIGNED'),
        ('cancel_picked', 'Cancelled after PICKED'),
        ('no_show_vendor', 'Vendor no-show'),
        ('no_deliver_driver', 'Driver did not deliver'),
    ])
    weight = models.IntegerField(default=1)
    waived = models.BooleanField(default=False)
    order = models.ForeignKey('orders.Order', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        status = ' (waived)' if self.waived else ''
        return f"{self.reason} weight={self.weight}{status}"
