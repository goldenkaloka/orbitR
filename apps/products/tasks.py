import os
from io import BytesIO

from apps.products.models import Food
from celery import shared_task
from django.core.files.base import ContentFile
from PIL import Image, ImageOps

# Grace period before the pre-optimization object is removed. Must outlive
# every cache layer that can still hold the old URL: 900 s restaurant list
# plus the 3600 s pre-warmed restaurant_mine payload, with margin.
PURGE_REPLACED_FOOD_IMAGE_GRACE_SECONDS = 4000


@shared_task
def process_food_image(food_id):
    """
    Background task to resize and optimize food images for mobile viewing.

    Output is WebP: at catalogue sizes it is visually equivalent to JPEG q85
    while weighing roughly half as much, and both the Flutter app and any
    future web surface decode it natively. Mirrors the marketplace pipeline
    (apps/marketplace/tasks.py) so the two stay in step.

    Uses save=False + _skip_signal to prevent post_save loop.
    """
    try:
        food = Food.objects.select_related('section__restaurant').get(id=food_id)
        if not food.image:
            return "No image to process"

        img = Image.open(food.image)
        # Vendors shoot on phones, so honour the orientation tag before resizing.
        img = ImageOps.exif_transpose(img)

        # Already within the size cap and already WebP — nothing to gain.
        if (
            food.image.name.lower().endswith('.webp')
            and img.width <= 1080
            and img.height <= 1080
        ):
            return f"Image for {food.name} already processed"

        # Instagram-style optimization: 1080x1080 max
        img.thumbnail((1080, 1080))

        thumb_io = BytesIO()
        img.convert('RGB').save(thumb_io, format='WEBP', quality=75, method=6)

        filename = os.path.basename(food.image.name)
        name_no_ext = os.path.splitext(filename)[0]
        webp_name = f"{name_no_ext}.webp"

        old_image_name = food.image.name

        # Update the image field without triggering signals again
        food.image.save(webp_name, ContentFile(thumb_io.getvalue()), save=False)
        food._skip_signal = True
        food.save(update_fields=['image'])

        # Two-phase delete: point the row at the new WebP now, but schedule
        # the old object's removal for after every cache layer has turned over.
        if old_image_name and old_image_name != food.image.name:
            purge_replaced_food_image.apply_async(
                args=[old_image_name],
                countdown=PURGE_REPLACED_FOOD_IMAGE_GRACE_SECONDS,
            )

        # Cached restaurant payloads embed the old URL — bump the list version
        # and re-warm the owner/staff mine caches with the new WebP.
        from api.views.core import invalidate_restaurant_cache
        invalidate_restaurant_cache(food.section.restaurant if food.section else None)

        return f"Processed image for {food.name}"
    except Exception as e:
        return f"Error processing image: {str(e)}"


@shared_task
def purge_replaced_food_image(old_image_name):
    """
    Delete a pre-optimization food image object once it is guaranteed to have
    aged out of every cache layer. No-op if the object was already removed or
    the name has since been reused by a newer upload.
    """
    from django.core.files.storage import default_storage

    try:
        if default_storage.exists(old_image_name):
            default_storage.delete(old_image_name)
    except Exception:
        # Purge is best-effort housekeeping — never fail the worker on it.
        pass
