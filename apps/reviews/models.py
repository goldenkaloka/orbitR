from django.db import models
from apps.users.models import User
from apps.orders.models import Order
from apps.restaurants.models import Restaurant
from apps.delivery.models import Driver
import uuid

class Review(models.Model):
    REVIEW_TYPE_CHOICES = (
        ('restaurant', 'Restaurant Review'),
        ('driver', 'Driver Review'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='reviews')
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews_given')
    
    review_type = models.CharField(max_length=20, choices=REVIEW_TYPE_CHOICES)
    
    # Target can be either a restaurant or a driver
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, null=True, blank=True, related_name='reviews')
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, null=True, blank=True, related_name='reviews')
    
    rating = models.PositiveSmallIntegerField() # 1 to 5
    comment = models.TextField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Prevent multiple reviews of the same type for the same order
        unique_together = ('order', 'review_type')
        indexes = [
            models.Index(fields=['restaurant', 'rating']),
            models.Index(fields=['driver', 'rating']),
        ]

    def __str__(self):
        return f"{self.review_type} Review - {self.rating} stars"
