from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.restaurants.models import Restaurant, RestaurantSection


DEFAULT_SECTION_NAME = 'Main Kitchen'


@receiver(post_save, sender=Restaurant)
def ensure_default_restaurant_section(sender, instance, created, **kwargs):
    if not created:
        return
    RestaurantSection.objects.get_or_create(
        restaurant=instance,
        name=DEFAULT_SECTION_NAME,
        defaults={
            'specialization': 'General',
            'description': 'Default preparation area',
            'sort_order': 0,
            'prep_time': instance.average_prep_time or 20,
        },
    )
