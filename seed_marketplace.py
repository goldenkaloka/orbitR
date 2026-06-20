import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.marketplace.models import MarketplaceCategory


def seed_categories():
    categories = [
        ('Electronics', 'cpu'),
        ('Clothing', 'shirt'),
        ('Books', 'book-open'),
        ('Furniture', 'sofa'),
        ('Services', 'wrench'),
        ('Others', 'package'),
    ]
    
    for name, icon in categories:
        cat, created = MarketplaceCategory.objects.get_or_create(name=name, defaults={'icon': icon})
        if created:
            print(f"Created category: {name}")
        else:
            print(f"Category already exists: {name}")

if __name__ == "__main__":
    seed_categories()
