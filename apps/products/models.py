from django.db import models
from apps.restaurants.models import Restaurant
import uuid

class Food(models.Model):
    CATEGORY_CHOICES = (
        ('POPULAR', 'Popular'),
        ('MAIN', 'Main'),
        ('SIDES', 'Sides'),
        ('DRINKS', 'Drinks'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vendor = models.ForeignKey(Restaurant, on_delete=models.CASCADE, related_name='foods', db_index=True)
    section = models.ForeignKey('restaurants.RestaurantSection', on_delete=models.SET_NULL, null=True, blank=True, related_name='foods')
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='products/', blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='MAIN', db_index=True)
    is_available = models.BooleanField(default=True, db_index=True)

    def __str__(self):
        return f"{self.name} - {self.vendor.name}"
