from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache
from apps.restaurants.models import Restaurant
from apps.products.models import Food

@receiver([post_save, post_delete], sender=Restaurant)
def invalidate_restaurant_cache(sender, instance, **kwargs):
    """
    Invalidate global and vendor-specific restaurant caches.
    """
    # 1. Clear global list
    cache.delete('restaurants_list_global')
    
    # 2. Clear owner's list
    if instance.owner:
        cache.delete(f'vendor_restaurants_{instance.owner.id}')
    
    # 3. Clear specific detail
    cache.delete(f'restaurant_detail_{instance.id}')

@receiver([post_save, post_delete], sender=Food)
def invalidate_food_cache(sender, instance, **kwargs):
    """
    Invalidate restaurant detail cache when food items change.
    """
    if hasattr(instance, 'vendor') and instance.vendor:
        cache.delete(f'restaurant_detail_{instance.vendor.id}')
