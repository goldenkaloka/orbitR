import io

import requests
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from PIL import Image, ImageOps

from apps.marketplace.models import MarketplaceProduct, ProductImage

API = "https://api.openverse.org/v1/images/"
# Every image is cropped and re-encoded below, which is a derivative work, so
# NoDerivatives terms are excluded outright. Anything left is commercially
# usable AND modifiable — these listings are a marketplace, so non-commercial
# or share-alike-with-ND mixes would not do.
SEARCH_PARAMS = {
    "license": "cc0,pdm,by,by-sa",
    "page_size": 8,
    "mature": "false",
}
HEADERS = {"User-Agent": "orbitr-storefront/1.0 (product catalogue)"}
TARGET = (800, 600)

# Generic search terms rather than the brand names in the listing titles —
# brand-accurate photos are rarely available under a commercial licence.
QUERIES = {
    "Motorbike Helmet DOT": "motorcycle helmet",
    "Bicycle 28\" City": "city bicycle",
    "Nissan Note 2019": "nissan car",
    "Toyota Hilux 2018": "pickup truck",
    "Refrigerator 350L": "refrigerator",
    "Air Fryer 5.5L": "air fryer",
    "Microwave 20L": "microwave oven",
    "Washing Machine 8kg": "washing machine",
    "Gas Cooker 4 Burner": "gas stove",
    "Mattress Queen 2x4": "mattress",
    "Bookshelf 5 Tier": "bookshelf",
    "Office Chair Ergonomic": "office chair",
    "Dining Table 6 Seater": "dining table",
    "3-Seater Fabric Sofa": "sofa",
    "Biology for Form 4": "biology textbook",
    "Set of 10 Novels": "stack of books",
    "Clean Architecture": "computer science book",
    "Kiswahili Kamuseli": "dictionary book",
    "Advanced Calculus 2nd Ed": "mathematics textbook",
    "Wool Blend Overcoat": "wool coat",
    "Kids School Uniform": "school uniform",
    "Bafana 2024 Jersey": "football jersey",
    "Levis 501 Jeans": "blue jeans",
    "Nike Air Max 90": "sneakers",
    "Kanzu Linen Set": "traditional clothing",
    "Logitech MX Master 3S": "computer mouse",
    "Anker 65W Charger": "phone charger",
    "iPad Air 5": "tablet computer",
    "Sony WH-1000XM5": "headphones",
    "Samsung Galaxy S23": "smartphone",
    "Laptop Pro 14\"": "laptop computer",
}


class Command(BaseCommand):
    help = (
        "Replace generated placeholder art with real photography fetched from "
        "Openverse (commercially licensed). Idempotent; use --only to redo one."
    )

    def add_arguments(self, parser):
        parser.add_argument("--only", help="Only process the listing with this name")
        parser.add_argument(
            "--keep-existing",
            action="store_true",
            help="Skip listings that already have an image",
        )

    def handle(self, *args, **options):
        products = MarketplaceProduct.objects.order_by("name")
        if options["only"]:
            products = products.filter(name=options["only"])

        ok, skipped, failed = 0, 0, []

        for product in products:
            if options["keep_existing"] and product.images.exists():
                skipped += 1
                continue

            query = QUERIES.get(product.name, product.name)
            result = self._fetch(query)

            if not result:
                failed.append(product.name)
                self.stdout.write(f"  MISS  {product.name}  (query: {query})")
                continue

            payload, source = result
            ProductImage.objects.filter(product=product).delete()
            ProductImage.objects.create(
                product=product,
                image=ContentFile(payload, name=f"{product.id}.jpg"),
            )
            ok += 1
            self.stdout.write(f"  OK    {product.name}  <- {source}")

        self.stdout.write(
            self.style.SUCCESS(
                f"\n{ok} image(s) replaced, {skipped} kept, "
                f"{len(failed)} failed."
            )
        )
        if failed:
            self.stdout.write("Failed: " + ", ".join(failed))

    def _fetch(self, query):
        """First result that downloads and decodes, normalised to 800x600."""
        try:
            search = requests.get(
                API,
                params={"q": query, **SEARCH_PARAMS},
                headers=HEADERS,
                timeout=30,
            )
            search.raise_for_status()
        except requests.RequestException as exc:
            self.stderr.write(f"search failed for {query!r}: {exc}")
            return None

        for hit in search.json().get("results", []):
            url = hit.get("url")
            if not url:
                continue
            try:
                raw = requests.get(url, headers=HEADERS, timeout=45).content
                photo = self._normalise(raw)
            except Exception:
                continue
            if photo is None:
                continue
            licence = hit.get("license", "?")
            source = hit.get("source") or hit.get("foreign_landing_url") or url
            return photo, f"{licence} via {source}"

        return None

    def _normalise(self, raw):
        """Decode, centre-crop to the card aspect ratio, re-encode as WebP."""
        try:
            image = Image.open(io.BytesIO(raw))
            image = ImageOps.exif_transpose(image).convert("RGB")
        except Exception:
            return None

        if image.width < 200 or image.height < 200:
            return None

        image = ImageOps.fit(image, TARGET, method=Image.LANCZOS)
        buf = io.BytesIO()
        # WebP matches JPEG visually at catalogue sizes while staying well
        # under half the weight, and both the storefront and Flutter decode it.
        image.save(buf, format="WEBP", quality=75, method=6)
        return buf.getvalue()
