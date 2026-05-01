import os
from celery import shared_task
from django.core.files.base import ContentFile
from apps.products.models import Food
from PIL import Image
from io import BytesIO

@shared_task
def process_food_image(food_id):
    """
    Background task to resize and optimize food images for mobile viewing.
    """
    try:
        food = Food.objects.get(id=food_id)
        if not food.image:
            return "No image to process"

        img = Image.open(food.image)
        
        # Instagram-style optimization: 1080x1080 max
        img.thumbnail((1080, 1080))
        
        thumb_io = BytesIO()
        img.save(thumb_io, format='JPEG', quality=85)
        
        # Update the image field without triggering signals again
        filename = os.path.basename(food.image.name)
        food.image.save(filename, ContentFile(thumb_io.getvalue()), save=False)
        food._skip_signal = True
        food.save(update_fields=['image'])        
        return f"Processed image for {food.name}"
    except Exception as e:
        return f"Error processing image: {str(e)}"
