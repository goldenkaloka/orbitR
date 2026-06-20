import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.marketplace.models import ProductImage
from django.core.files.uploadedfile import SimpleUploadedFile

# Create a dummy image
img = SimpleUploadedFile("test_img.jpg", b"file_content", content_type="image/jpeg")

# Create product
from apps.marketplace.models import MarketplaceCategory, MarketplaceProduct
from apps.users.models import User

user = User.objects.first()
if not user:
    user = User.objects.create(username='test_user', phone='+123456')

cat = MarketplaceCategory.objects.first()
if not cat:
    cat = MarketplaceCategory.objects.create(name='Test Cat')

prod = MarketplaceProduct.objects.first()
if not prod:
    prod = MarketplaceProduct.objects.create(
        seller=user,
        category=cat,
        name='Test Product',
        description='Desc',
        price=10.0,
        whatsapp_number='+123456'
    )

pi = ProductImage.objects.create(product=prod, image=img)
print("Initial image name:", pi.image.name)
print("Image changed flag after create:", getattr(pi, '_image_changed', None))

# Fetch again
pi2 = ProductImage.objects.get(id=pi.id)
pi2.is_primary = not pi2.is_primary
pi2.save()

print("Image changed flag after save:", getattr(pi2, '_image_changed', None))

# Process image
from apps.marketplace.tasks import process_product_image

print(process_product_image(str(pi.id)))

pi3 = ProductImage.objects.get(id=pi.id)
print("Processed image name:", pi3.image.name)

# Now what if process_product_image runs again?
print(process_product_image(str(pi.id)))

pi4 = ProductImage.objects.get(id=pi.id)
print("Processed image name 2:", pi4.image.name)

pi.delete()
