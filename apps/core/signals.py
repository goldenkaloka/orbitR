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

@receiver([post_save, post_delete], sender=Restaurant)
def invalidate_restaurant_cache(sender, instance, **kwargs):
    """Invalidate restaurant 'mine' and list cache when updated."""
    cache.delete(f"restaurant_mine_{instance.owner.id}")
    try:
        cache.delete_pattern("restaurant_list:*")
    except AttributeError:
        # delete_pattern is specific to django-redis
        cache.clear()

@receiver([post_save, post_delete], sender=Food)
def invalidate_menu_cache(sender, instance, **kwargs):
    """Invalidate restaurant cache when menu items change."""
    cache.delete(f"restaurant_mine_{instance.vendor.owner.id}")
    try:
        cache.delete_pattern("restaurant_list:*")
    except AttributeError:
        cache.clear()
