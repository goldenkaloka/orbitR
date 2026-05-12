from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache
from apps.orders.models import Order
from apps.restaurants.models import Restaurant
from apps.products.models import Food

@receiver([post_save, post_delete], sender=Order)
def invalidate_order_stats(sender, instance, **kwargs):
    """Invalidate stats cache when orders are updated."""
    # Invalidate for vendor
    if instance.restaurant:
        cache.delete(f"user_stats_{instance.restaurant.owner.id}_vendor")
    # Invalidate for driver
    if instance.driver:
        cache.delete(f"user_stats_{instance.driver.user.id}_driver")
    # Invalidate for admin
    cache.delete_pattern("user_stats_*_admin")

@receiver([post_save, post_delete], sender=Order)
def invalidate_order_stats(sender, instance, **kwargs):
    """Invalidate stats cache when orders are updated."""
    # Invalidate for vendor
    if instance.restaurant:
        cache.delete(f"user_stats_{instance.restaurant.owner.id}_vendor")
    # Invalidate for driver
    if instance.driver:
        cache.delete(f"user_stats_{instance.driver.user.id}_driver")
    # Invalidate for admin
    cache.delete_pattern("user_stats_*_admin")
