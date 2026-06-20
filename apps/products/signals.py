from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import Food
from .tasks import process_food_image


@receiver(pre_save, sender=Food)
def check_food_image_changed(sender, instance, **kwargs):
    """
    Check if the image field is being updated.
    Flags _image_changed on the instance.
    """
    if not instance.pk:
        instance._image_changed = True
    else:
        try:
            old_instance = Food.objects.get(pk=instance.pk)
            instance._image_changed = (old_instance.image != instance.image)
        except Food.DoesNotExist:
            instance._image_changed = True

@receiver(post_save, sender=Food)
def trigger_image_processing(sender, instance, created, **kwargs):
    """
    Triggers background image optimization when a food item is created or updated.
    Checks if an image is present and if it's the first save (or could be enhanced to check for changes).
    """
    if instance.image and not getattr(instance, '_skip_signal', False):
        # We use .delay() to send it to the Celery worker queue
        if created or getattr(instance, '_image_changed', False):
            process_food_image.delay(str(instance.id))
