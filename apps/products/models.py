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
    section = models.ForeignKey('restaurants.RestaurantSection', on_delete=models.CASCADE, related_name='foods', db_index=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='products/', blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='MAIN', db_index=True)
    is_available = models.BooleanField(default=True, db_index=True)

    @property
    def vendor(self):
        return self.section.restaurant

    @property
    def vendor_id(self):
        return self.section.restaurant_id

    def __str__(self):
        return f"{self.name} - {self.section.restaurant.name}"
