import logging
import os
from datetime import timedelta
from io import BytesIO

from celery import shared_task
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image


@shared_task
def expire_demand(demand_id):
    from domain.services.demand_services import DemandService
    DemandService.expire_demand(demand_id)


@shared_task
def process_product_image(image_id):
    """
    Background task to resize and optimize marketplace product images for mobile viewing.
    Mirrors the food image processing pipeline.
    Uses save=False + _skip_signal to prevent post_save loop.
    """
    try:
        from apps.marketplace.models import ProductImage
        pi = ProductImage.objects.select_related('product').get(id=image_id)
        if not pi.image:
            return "No image to process"

        img = Image.open(pi.image)

        # Check if already processed (correct size and format)
        is_already_jpg = pi.image.name.lower().endswith('.jpg')
        if is_already_jpg and img.width <= 1080 and img.height <= 1080 and img.format in ('JPEG', 'MPO'):
            return f"Image {image_id} already processed"

        # Resize to 1080x1080 max (same as food images)
        img.thumbnail((1080, 1080))

        thumb_io = BytesIO()
        img.save(thumb_io, format='JPEG', quality=85)

        filename = os.path.basename(pi.image.name)
        # Ensure .jpg extension
        name_no_ext = os.path.splitext(filename)[0]
        jpg_name = f"{name_no_ext}.jpg"

        old_image_name = pi.image.name

        # Save without triggering post_save signal again
        pi.image.save(jpg_name, ContentFile(thumb_io.getvalue()), save=False)
        
        # Prevent orphan files
        if old_image_name and old_image_name != pi.image.name:
            from django.core.files.storage import default_storage
            if default_storage.exists(old_image_name):
                default_storage.delete(old_image_name)

        pi._skip_signal = True
        pi.save(update_fields=['image'])
        return f"Processed image {image_id} for product {pi.product.name}"
    except Exception as e:
        return f"Error processing marketplace image {image_id}: {str(e)}"


@shared_task
def cleanup_unpaid_products():
    """
    Deletes marketplace products stuck in PENDING_PAYMENT for more than 24 hours.
    These are products created without completing the listing fee payment.
    """
    from apps.marketplace.models import MarketplaceProduct
    threshold = timezone.now() - timedelta(hours=24)

    with transaction.atomic():
        abandoned = list(
            MarketplaceProduct.objects.select_for_update(skip_locked=True)
            .filter(status='PENDING_PAYMENT', created_at__lt=threshold)
        )

        if not abandoned:
            return f"Cleaned up 0 unpaid products."

        logger = logging.getLogger(__name__)
        logger.info(f"[MarketplaceCleanup] Deleting {len(abandoned)} abandoned PENDING_PAYMENT products")

        for product in abandoned:
            try:
                product.delete()
            except Exception as e:
                logger.error(f"[MarketplaceCleanup] Failed to delete product {product.id}: {e}")

    return f"Deleted {len(abandoned)} unpaid products."
