from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Food
from .tasks import process_food_image

@receiver(post_save, sender=Food)
def trigger_image_processing(sender, instance, created, **kwargs):
    """
    Triggers background image optimization when a food item is created or updated.
    Checks if an image is present and if it's the first save (or could be enhanced to check for changes).
    """
    if instance.image and not getattr(instance, '_skip_signal', False):
        # We use .delay() to send it to the Celery worker queue
        process_food_image.delay(str(instance.id))
