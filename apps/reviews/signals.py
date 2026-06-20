from django.db.models import Avg
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Review


@receiver(post_save, sender=Review)
def update_aggregate_rating(sender, instance, **kwargs):
    if instance.review_type == 'restaurant' and instance.restaurant:
        avg_rating = Review.objects.filter(
            restaurant=instance.restaurant, 
            review_type='restaurant'
        ).aggregate(Avg('rating'))['rating__avg']
        
        instance.restaurant.rating = avg_rating or 0.0
        instance.restaurant.save()
        
    elif instance.review_type == 'driver' and instance.driver:
        avg_rating = Review.objects.filter(
            driver=instance.driver, 
            review_type='driver'
        ).aggregate(Avg('rating'))['rating__avg']
        
        instance.driver.rating = avg_rating or 0.0
        instance.driver.save()
