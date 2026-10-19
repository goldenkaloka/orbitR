import hashlib
import io
import random

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from PIL import Image, ImageDraw

from apps.marketplace.models import MarketplaceProduct, ProductImage

# Palettes keep listings distinguishable in a grid without shipping real art.
PALETTES = [
    ((36, 64, 98), (86, 152, 214)),
    ((94, 53, 77), (194, 123, 161)),
    ((38, 94, 77), (111, 194, 168)),
    ((94, 76, 38), (214, 180, 103)),
    ((60, 46, 94), (139, 111, 214)),
    ((94, 45, 45), (214, 111, 111)),
]


class Command(BaseCommand):
    help = (
        "Generate a placeholder image for every marketplace listing that has "
        "none, written through the configured object storage. Idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate images even for listings that already have one",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Only process this many listings (0 = all)",
        )

    def handle(self, *args, **options):
        products = MarketplaceProduct.objects.prefetch_related("images").order_by(
            "id"
        )
        if not options["force"]:
            products = products.filter(images__isnull=True).distinct()

        created = 0
        for product in products[: options["limit"] or None]:
            payload = self._render(product)
            ProductImage.objects.create(
                product=product,
                image=ContentFile(
                    payload, name=f"{product.id}.jpg"
                ),
            )
            created += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Attached images to {created} listing(s). "
                f"Total images now: {ProductImage.objects.count()}"
            )
        )

    def _render(self, product):
        """A 800x600 JPEG keyed off the listing so each one looks distinct."""
        width, height = 800, 600
        digest = hashlib.sha256(str(product.id).encode()).hexdigest()
        dark, light = PALETTES[int(digest[:2], 16) % len(PALETTES)]
        rng = random.Random(digest)

        image = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(image)

        # Vertical wash so flat blocks do not look like broken tiles.
        for y in range(height):
            t = y / height
            draw.line(
                (
                    0,
                    y,
                    width,
                    y,
                ),
                fill=tuple(
                    int(dark[i] + (light[i] - dark[i]) * t) for i in range(3)
                ),
            )

        # A few translucent shapes give the tile some structure.
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        shapes = ImageDraw.Draw(overlay)
        for _ in range(6):
            x = rng.randint(-100, width)
            y = rng.randint(-100, height)
            r = rng.randint(60, 220)
            shapes.ellipse(
                (x, y, x + r, y + r),
                fill=(255, 255, 255, rng.randint(12, 28)),
            )
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")

        draw = ImageDraw.Draw(image)
        name = (product.name or "Listing").strip()
        draw.text((40, height - 90), name[:48], fill=(255, 255, 255))
        draw.text(
            (40, height - 60),
            (product.description or "").strip()[:60],
            fill=(230, 230, 230),
        )

        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=82, optimize=True)
        return buf.getvalue()
