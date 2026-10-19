import io
import os

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.core.files.storage import default_storage
from PIL import Image, ImageOps

from apps.marketplace.models import ProductImage

# WebP at q75 is visually equivalent to JPEG q85 for catalogue photography
# while typically landing 45-60% smaller — which matters twice here, because
# the same object is fetched by the storefront and by the mobile app.
DEFAULT_MAX_WIDTH = 900
DEFAULT_QUALITY = 75


class Command(BaseCommand):
    help = (
        "Re-encode marketplace images as WebP at a catalogue-appropriate size. "
        "Idempotent; use --force to reprocess already-compressed images."
    )

    def add_arguments(self, parser):
        parser.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH)
        parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY)
        parser.add_argument(
            "--force",
            action="store_true",
            help="Reprocess even images that are already WebP within the limit",
        )

    def handle(self, *args, **options):
        max_width, quality = options["max_width"], options["quality"]
        images = ProductImage.objects.select_related("product").order_by("id")

        done = skipped = failed = 0
        before_total = after_total = 0

        for image in images:
            name = image.image.name or ""
            if not name:
                continue

            already_webp = name.lower().endswith(".webp")
            if already_webp and not options["force"]:
                skipped += 1
                continue

            try:
                with default_storage.open(name, "rb") as fh:
                    raw = fh.read()
                before = len(raw)
                payload = self._encode(raw, max_width, quality)
            except Exception as exc:
                failed += 1
                self.stderr.write(f"  FAIL  {name}: {exc}")
                continue

            if not payload:
                failed += 1
                self.stderr.write(f"  FAIL  {name}: could not decode")
                continue

            base = os.path.splitext(os.path.basename(name))[0]
            new_name = f"{base}.webp"
            image.image.save(new_name, ContentFile(payload), save=False)
            image.save(update_fields=["image"])

            if image.image.name != name and default_storage.exists(name):
                default_storage.delete(name)

            after = len(payload)
            before_total += before
            after_total += after
            done += 1
            saved = 100 - (after * 100 // before) if before else 0
            self.stdout.write(
                f"  OK    {image.product.name[:28]:28} "
                f"{before // 1024:>5} KB -> {after // 1024:>4} KB  ({saved}% off)"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"\n{done} compressed, {skipped} already webp, {failed} failed."
            )
        )
        if before_total:
            self.stdout.write(
                f"Total {before_total // 1024} KB -> {after_total // 1024} KB "
                f"({100 - (after_total * 100 // before_total)}% smaller)."
            )

    def _encode(self, raw, max_width, quality):
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image).convert("RGB")

        if image.width > max_width:
            ratio = max_width / image.width
            image = image.resize(
                (max_width, max(1, int(image.height * ratio))),
                Image.LANCZOS,
            )

        buf = io.BytesIO()
        image.save(buf, format="WEBP", quality=quality, method=6)
        return buf.getvalue()
