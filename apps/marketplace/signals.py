from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import ProductImage
from .tasks import process_product_image


@receiver(post_save, sender=ProductImage)
def trigger_marketplace_image_processing(sender, instance, created, **kwargs):
    """
    Triggers background image optimization when a product image is created.
    Processed asynchronously via Celery.
    """
    if instance.image and not getattr(instance, '_skip_signal', False):
        process_product_image.delay(str(instance.id))
