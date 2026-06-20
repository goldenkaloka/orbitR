import os
import logging
from django.db.models.signals import pre_save, post_save, post_delete
from django.dispatch import receiver
from django.core.files.storage import default_storage
from .models import ProductImage
from .tasks import process_product_image

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=ProductImage)
def check_image_changed(sender, instance, **kwargs):
    """
    Check if the image field is being updated.
    Flags _image_changed on the instance.
    """
    if not instance.pk:
        instance._image_changed = True
    else:
        try:
            old_instance = ProductImage.objects.get(pk=instance.pk)
            instance._image_changed = (old_instance.image != instance.image)
        except ProductImage.DoesNotExist:
            instance._image_changed = True


@receiver(post_save, sender=ProductImage)
def trigger_marketplace_image_processing(sender, instance, created, **kwargs):
    """
    Triggers background image optimization when a product image is created or updated.
    Processed asynchronously via Celery.
    """
    if instance.image and not getattr(instance, '_skip_signal', False):
        if created or getattr(instance, '_image_changed', False):
            process_product_image.delay(str(instance.id))


@receiver(post_delete, sender=ProductImage)
def cleanup_marketplace_image_file(sender, instance, **kwargs):
    """
    Delete the actual image file from storage when a ProductImage is deleted.
    Prevents orphaned files accumulating in storage.
    """
    if instance.image and default_storage.exists(instance.image.name):
        try:
            default_storage.delete(instance.image.name)
            logger.info(f"Deleted orphaned marketplace image: {instance.image.name}")
        except Exception as e:
            logger.error(f"Failed to delete marketplace image {instance.image.name}: {e}")
