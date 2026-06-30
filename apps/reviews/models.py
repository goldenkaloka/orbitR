import uuid

from apps.delivery.models import Driver
from apps.orders.models import Order
from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.db import models


class Review(models.Model):
    REVIEW_TYPE_CHOICES = (
        ('restaurant', 'Restaurant Review'),
        ('driver', 'Driver Review'),
        ('marketplace', 'Marketplace Product Review'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='reviews', null=True, blank=True)
    customer = models.ForeignKey(User, on_delete=models.CASCADE, related_name='reviews_given')
    
    review_type = models.CharField(max_length=20, choices=REVIEW_TYPE_CHOICES)
    
    # Targets
    restaurant = models.ForeignKey(Restaurant, on_delete=models.CASCADE, null=True, blank=True, related_name='reviews')
    driver = models.ForeignKey(Driver, on_delete=models.CASCADE, null=True, blank=True, related_name='reviews')
    marketplace_product = models.ForeignKey('marketplace.MarketplaceProduct', on_delete=models.CASCADE, null=True, blank=True, related_name='reviews')
    
    rating = models.PositiveSmallIntegerField() # 1 to 5
    comment = models.TextField(blank=True, null=True)
    is_verified_purchase = models.BooleanField(default=False, db_index=True)
    is_visible = models.BooleanField(default=True, db_index=True)
    moderation_status = models.CharField(
        max_length=20,
        choices=(
            ('APPROVED', 'Approved'),
            ('PENDING', 'Pending'),
            ('REJECTED', 'Rejected'),
        ),
        default='APPROVED',
        db_index=True,
    )
    
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Standard order reviews (one per order type)
            models.UniqueConstraint(
                fields=['order', 'review_type'],
                name='unique_order_review',
                condition=models.Q(order__isnull=False)
            ),
            # Marketplace product reviews (one per customer per product)
            models.UniqueConstraint(
                fields=['customer', 'marketplace_product'],
                name='unique_marketplace_product_review',
                condition=models.Q(marketplace_product__isnull=False)
            )
        ]
        indexes = [
            models.Index(fields=['restaurant', 'rating']),
            models.Index(fields=['restaurant', 'is_visible', 'is_verified_purchase', 'created_at']),
            models.Index(fields=['driver', 'rating']),
            models.Index(fields=['driver', 'is_visible', 'is_verified_purchase', 'created_at']),
            models.Index(fields=['marketplace_product', 'rating']),
            models.Index(fields=['marketplace_product', 'is_visible', 'is_verified_purchase', 'created_at']),
        ]

    def __str__(self):
        return f"{self.review_type} Review - {self.rating} stars"
