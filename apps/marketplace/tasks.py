import logging
import os
from datetime import timedelta
from io import BytesIO

from celery import shared_task
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageOps

# How long a pre-optimization storage object survives after its replacement is
# written. Must exceed every cache layer that could still hold the old URL:
# 900s Django product-list + 60s Next storefront, with margin.
PURGE_REPLACED_IMAGE_GRACE_SECONDS = 1800


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

    Output is WebP: at catalogue sizes it is visually equivalent to JPEG q85
    while weighing roughly half as much, and both the storefront and the
    Flutter app decode it natively.

    Deletion of the pre-optimization object is DEFERRED (see
    ``purge_replaced_product_image``): the product-list response is cached for
    900s and the storefront holds its own 60s copy, so deleting the old object
    immediately would leave both layers rendering a dangling URL.
    """
    try:
        from apps.marketplace.models import ProductImage
        pi = ProductImage.objects.select_related('product').get(id=image_id)
        if not pi.image:
            return "No image to process"

        img = Image.open(pi.image)
        # Sellers shoot on phones, so honour the orientation tag before resizing.
        img = ImageOps.exif_transpose(img)

        # Already within the size cap and already WebP — nothing to gain.
        if (
            pi.image.name.lower().endswith('.webp')
            and img.width <= 1080
            and img.height <= 1080
        ):
            return f"Image {image_id} already processed"

        # 1080x1080 max (same as food images)
        img.thumbnail((1080, 1080))

        thumb_io = BytesIO()
        img.convert('RGB').save(thumb_io, format='WEBP', quality=75, method=6)

        filename = os.path.basename(pi.image.name)
        name_no_ext = os.path.splitext(filename)[0]
        webp_name = f"{name_no_ext}.webp"

        old_image_name = pi.image.name

        # Save without triggering post_save signal again
        pi.image.save(webp_name, ContentFile(thumb_io.getvalue()), save=False)

        # Two-phase delete: point the row at the new WebP now, but schedule the
        # old object's removal for after every cache layer has turned over.
        if old_image_name and old_image_name != pi.image.name:
            purge_replaced_product_image.apply_async(
                args=[old_image_name],
                countdown=PURGE_REPLACED_IMAGE_GRACE_SECONDS,
            )

        pi._skip_signal = True
        pi.save(update_fields=['image'])

        # The list response is cached with the old image URL for 900s — bump the
        # version so the next read serves the new WebP.
        from domain.services.marketplace_services import invalidate_product_list_cache
        invalidate_product_list_cache()

        return f"Processed image {image_id} for product {pi.product.name}"
    except Exception as e:
        return f"Error processing marketplace image {image_id}: {str(e)}"


@shared_task
def purge_replaced_product_image(old_image_name):
    """
    Delete a pre-optimization storage object once it is guaranteed to have aged
    out of every cache layer.

    Deferred rather than immediate: the Django product list caches its rendered
    response (including the image URL) for 900s and the Next storefront holds a
    further 60s copy, so an immediate delete would leave both layers rendering a
    404. The grace period covers both with margin. If the object was already
    removed or the name has since been reused by a newer upload, this is a no-op.
    """
    from django.core.files.storage import default_storage

    try:
        if default_storage.exists(old_image_name):
            default_storage.delete(old_image_name)
            return f"Purged replaced image {old_image_name}"
        return f"Replaced image {old_image_name} already gone"
    except Exception as e:
        # Never let a storage hiccup fail the pipeline — the object simply
        # becomes an orphan and is caught by storage-level lifecycle rules.
        logging.getLogger(__name__).warning(
            f"Could not purge replaced image {old_image_name}: {e}"
        )
        return f"Skipped purge of {old_image_name}: {e}"


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


@shared_task
def sweep_expired_products():
    """
    Daily task to transition products past their expiry date to EXPIRED.
    """
    from domain.services.marketplace_services import MarketplaceService
    count = MarketplaceService.mark_expired_products()
    return f"Auto-expired {count} products."
