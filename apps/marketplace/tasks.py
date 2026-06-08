import os
from celery import shared_task
from django.core.files.base import ContentFile
from PIL import Image
from io import BytesIO


@shared_task
def expire_demand(demand_id):
    from domain.services.demand_services import DemandService
    DemandService.expire_demand(demand_id)


@shared_task
def process_product_image(image_id):
    """
    Background task to resize and optimize marketplace product images for mobile viewing.
    Mirrors the food image processing pipeline.
    """
    try:
        from apps.marketplace.models import ProductImage
        pi = ProductImage.objects.select_related('product').get(id=image_id)
        if not pi.image:
            return "No image to process"

        img = Image.open(pi.image)

        # Resize to 1080x1080 max (same as food images)
        img.thumbnail((1080, 1080))

        thumb_io = BytesIO()
        img.save(thumb_io, format='JPEG', quality=85)

        filename = os.path.basename(pi.image.name)
        # Ensure .jpg extension
        name_no_ext = os.path.splitext(filename)[0]
        jpg_name = f"{name_no_ext}.jpg"

        pi.image.save(jpg_name, ContentFile(thumb_io.getvalue()), save=True)
        return f"Processed image {image_id} for product {pi.product.name}"
    except Exception as e:
        return f"Error processing marketplace image {image_id}: {str(e)}"
