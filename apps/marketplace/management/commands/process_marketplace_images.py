from django.core.management.base import BaseCommand
from apps.marketplace.models import ProductImage
from apps.marketplace.tasks import process_product_image


class Command(BaseCommand):
    help = 'Process all existing marketplace product images through the resize pipeline'

    def handle(self, *args, **options):
        qs = ProductImage.objects.select_related('product')
        total = qs.count()
        self.stdout.write(f"Found {total} product images to process")

        processed = 0
        for pi in qs:
            result = process_product_image(str(pi.id))
            self.stdout.write(f"  {pi.id}: {result}")
            processed += 1

        self.stdout.write(self.style.SUCCESS(f"Processed {processed}/{total} images"))
