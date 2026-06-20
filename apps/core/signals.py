from apps.orders.models import Order
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver


# Fix #3 — removed duplicate @receiver registration.
# The original file registered invalidate_order_stats TWICE with identical
# decorators, causing double cache invalidation on every Order save/delete.
@receiver([post_save, post_delete], sender=Order)
def invalidate_order_stats(sender, instance, **kwargs):
    """Invalidate stats cache when orders are created, updated, or deleted."""
    if instance.restaurant_id:
        try:
            cache.delete(f"user_stats_{instance.restaurant.owner_id}_vendor")
        except Exception:
            pass
    if instance.driver_id:
        try:
            cache.delete(f"user_stats_{instance.driver.user_id}_driver")
        except Exception:
            pass
    cache.delete_pattern("user_stats_*_admin")

