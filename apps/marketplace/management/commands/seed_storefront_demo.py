"""
Seed a believable marketplace catalogue for local development and for
previewing the storefront.

Idempotent: re-running updates the existing rows instead of duplicating them.

    python manage.py seed_storefront_demo
"""

import random
from decimal import Decimal

from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.marketplace.models import MarketplaceCategory, MarketplaceProduct
from apps.users.models import User

# Dar es Salaam, roughly University area like the rest of the dev fixtures.
LAT, LNG = -6.7924, 39.2083

CATEGORIES = [
    ('Elektroniki', 'cpu', ['Laptop Pro 14"', 'Samsung Galaxy S23', 'Sony WH-1000XM5', 'iPad Air 5', 'Anker 65W Charger', 'Logitech MX Master 3S']),
    ('Mavazi', 'shirt', ['Kanzu Linen Set', 'Nike Air Max 90', 'Levis 501 Jeans', 'Bafana 2024 Jersey', 'Kids School Uniform', 'Wool Blend Overcoat']),
    ('Vitabu', 'book-open', ['Advanced Calculus 2nd Ed', 'Kiswahili Kamuseli', 'Clean Architecture', 'Set of 10 Novels', 'Biology for Form 4']),
    ('Samani', 'sofa', ['3-Seater Fabric Sofa', 'Dining Table 6 Seater', 'Office Chair Ergonomic', 'Bookshelf 5 Tier', 'Mattress Queen 2x4']),
    ('Vyombo vya Nyumbani', 'home', ['Gas Cooker 4 Burner', 'Washing Machine 8kg', 'Microwave 20L', 'Air Fryer 5.5L', 'Refrigerator 350L']),
    ('Vifaa vya Usafiri', 'car', ['Toyota Hilux 2018', 'Nissan Note 2019', 'Bicycle 28" City', 'Motorbike Helmet DOT']),
]

# (name, description, price in TSh)
ITEMS = [
    ('Laptop Pro 14"', 'Intel Core i7, 16GB RAM, 512GB SSD. Like new, boxed with charger.', 1450000),
    ('Samsung Galaxy S23', '128GB, black, under warranty. Battery health 94%.', 1850000),
    ('Sony WH-1000XM5', 'Industry leading noise cancelling headphones. Full set.', 1150000),
    ('iPad Air 5', '64GB WiFi, midnight. Scratch-free screen.', 2200000),
    ('Anker 65W Charger', '3-port GaN wall charger. Used twice.', 45000),
    ('Logitech MX Master 3S', 'Quiet clicks, USB-C charging. Barely used.', 68000),

    ('Kanzu Linen Set', 'Traditional kanzu and shirt in pure linen, size 34.', 95000),
    ('Nike Air Max 90', 'OG colourway, US 8.5. Worn twice.', 180000),
    ('Levis 501 Jeans', 'Classic straight fit, 32x34, no rips.', 75000),
    ('Bafana 2024 Jersey', 'Official match jersey, size M. New with tags.', 65000),
    ('Kids School Uniform', 'Complete set: 2 shirts, skirt, tie, socks. Size 12.', 55000),
    ('Wool Blend Overcoat', 'Winter coat, charcoal, size L. Dry clean only.', 110000),

    ('Advanced Calculus 2nd Ed', 'University textbook, minimal highlighting.', 85000),
    ('Kiswahili Kamuseli', 'Kielezo cha Kiswahili sanifu, hadithi na methali.', 45000),
    ('Clean Architecture', 'Robert C. Martin. Like new.', 60000),
    ('Set of 10 Novels', 'Mixed genre bundle. Great for kids library.', 70000),
    ('Biology for Form 4', 'Secondary chemistry/biology, good condition.', 35000),

    ('3-Seater Fabric Sofa', 'Grey, barely used. Delivery available in Dar.', 850000),
    ('Dining Table 6 Seater', 'Solid wood top with 6 chairs.', 1200000),
    ('Office Chair Ergonomic', 'Lumbar support, adjustable arms. Excellent condition.', 180000),
    ('Bookshelf 5 Tier', 'Sturdy pine, 180cm tall.', 95000),
    ('Mattress Queen 2x4', 'Orthopaedic, 10 year warranty, still covered.', 420000),

    ('Gas Cooker 4 Burner', 'LPG/electric convertible, auto ignition.', 480000),
    ('Washing Machine 8kg', 'Front load, inverter motor, 1 year old.', 950000),
    ('Microwave 20L', 'Digital, 800W, fits large plates.', 165000),
    ('Air Fryer 5.5L', 'Digital touchscreen, 8 presets.', 220000),
    ('Refrigerator 350L', 'Double door, frost free, energy efficient.', 1350000),

    ('Toyota Hilux 2018', '2.4 diesel, 4x4, 120000km, papers ready.', 68000000),
    ('Nissan Note 2019', 'Hybrid, low mileage, one owner.', 31000000),
    ('Bicycle 28" City', 'Steel frame, 21 speed, mudguards included.', 285000),
    ('Motorbike Helmet DOT', 'Full face, size M, no scratches.', 85000),
]


class Command(BaseCommand):
    help = 'Seeds a demo marketplace catalogue (idempotent)'

    def add_arguments(self, parser):
        parser.add_argument('--reset', action='store_true',
                            help='Delete existing demo products first')

    def handle(self, *args, **options):
        random.seed(7)

        if options['reset']:
            MarketplaceProduct.objects.all().delete()
            self.stdout.write(self.style.WARNING('Cleared existing products'))

        # Seller phone numbers are unique, so generate per run.
        sellers = []
        for i in range(6):
            phone = f'+25570{1000000 + i}'
            user, created = User.objects.get_or_create(
                phone=phone,
                defaults={'username': f'demo_seller_{i + 1}', 'role': 'vendor'},
            )
            if created:
                user.set_unusable_password()
                user.save(update_fields=['password'])
            sellers.append(user)

        categories = {}
        for name, icon, _items in CATEGORIES:
            category, _ = MarketplaceCategory.objects.get_or_create(
                name=name, defaults={'icon': icon},
            )
            categories[name] = category

        expiry = timezone.now() + timezone.timedelta(days=21)
        created_count = 0

        for name, description, price in ITEMS:
            category_name = self._category_for(name)
            seller = sellers[hash(name) % len(sellers)]
            # Spread sellers across a ~1.5km area so distance sorting is visible.
            jitter = lambda: random.uniform(-0.008, 0.008)
            point = Point(LNG + jitter(), LAT + random.uniform(-0.008, 0.008))

            product, created = MarketplaceProduct.objects.update_or_create(
                seller=seller,
                name=name,
                defaults={
                    'category': categories.get(category_name),
                    'description': description,
                    'price': Decimal(str(price)),
                    'condition': random.choice(
                        ['NEW', 'LIKE_NEW', 'GOOD', 'FAIR']
                    ),
                    'whatsapp_number': seller.phone,
                    'location': point,
                    'status': 'ACTIVE',
                    'is_fee_paid': True,
                    'expiry_date': expiry,
                },
            )
            if created:
                created_count += 1

        total = MarketplaceProduct.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f'Seeded {created_count} new products ({total} total)'
        ))
        self.stdout.write(f'Categories: {len(categories)}')
        self.stdout.write(f'Sellers:    {len(sellers)}')

    @staticmethod
    def _category_for(item_name):
        for category_name, _icon, items in CATEGORIES:
            if item_name in items:
                return category_name
        return 'Others'