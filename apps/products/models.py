import uuid

from apps.restaurants.models import Restaurant
from django.db import models


class Food(models.Model):
    CATEGORY_CHOICES = (
        ('POPULAR', 'Popular'),
        ('MAIN', 'Main'),
        ('SIDES', 'Sides'),
        ('DRINKS', 'Drinks'),
    )

    # Which serving window a dish belongs to. The first three values mirror
    # MealPeriod.KIND_CHOICES so a dish can be matched against the restaurant's
    # bookable windows; ALL_DAY dishes are sold at any time and always shown.
    BREAKFAST = 'BREAKFAST'
    LUNCH = 'LUNCH'
    DINNER = 'DINNER'
    ALL_DAY = 'ALL_DAY'
    MEAL_CHOICES = (
        (BREAKFAST, 'Breakfast'),
        (LUNCH, 'Lunch'),
        (DINNER, 'Dinner'),
        (ALL_DAY, 'All day'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    section = models.ForeignKey('restaurants.RestaurantSection', on_delete=models.CASCADE, related_name='foods', db_index=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='products/', blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='MAIN', db_index=True)
    meal_period = models.CharField(
        max_length=16,
        choices=MEAL_CHOICES,
        default=ALL_DAY,
        db_index=True,
        help_text="Serving window this dish belongs to; ALL_DAY is available at any time.",
    )
    # Promotion flag. Kept separate from `category` so "popular" never competes
    # with the real dish types (main/sides/drinks).
    is_popular = models.BooleanField(default=False, db_index=True)
    is_available = models.BooleanField(default=True, db_index=True)

    @property
    def vendor(self):
        return self.section.restaurant

    @property
    def vendor_id(self):
        return self.section.restaurant_id

    def __str__(self):
        return f"{self.name} - {self.section.restaurant.name}"
