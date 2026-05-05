from django.core.management.base import BaseCommand
from apps.marketplace.models import MarketplaceCategory

class Command(BaseCommand):
    help = 'Seeds initial marketplace categories'

    def handle(self, *args, **kwargs):
        categories = [
            ('Electronics', 'cpu'),
            ('Clothing', 'shirt'),
            ('Books', 'book-open'),
            ('Furniture', 'sofa'),
            ('Services', 'wrench'),
            ('Others', 'package'),
        ]
        
        self.stdout.write(self.style.SUCCESS('🚀 Seeding Marketplace Categories...'))
        
        for name, icon in categories:
            cat, created = MarketplaceCategory.objects.get_or_create(
                name=name, 
                defaults={'icon': icon}
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f"✅ Created category: {name}"))
            else:
                self.stdout.write(self.style.WARNING(f"ℹ️ Category already exists: {name}"))

        self.stdout.write(self.style.SUCCESS('🎉 Seeding completed successfully!'))
