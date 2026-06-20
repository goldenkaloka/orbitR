import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
import django

django.setup()
from apps.marketplace.models import MarketplaceCategory

cats = [
    ('Elektroniki', 'cpu'),
    ('Mavazi', 'shirt'),
    ('Vitabu', 'book-open'),
    ('Samani', 'sofa'),
    ('Vyombo vya Nyumbani', 'home'),
    ('Chakula na Vinywaji', 'utensils-crossed'),
    ('Simu na Kompyuta', 'smartphone'),
    ('Vifaa vya Michezo', 'dumbbell'),
    ('Vito na Mapambo', 'gem'),
    ('Mboga na Matunda', 'leaf'),
    ('Vifaa vya Watoto', 'baby'),
    ('Afya na Dawa', 'pill'),
    ('Magari na Vyombo', 'car'),
    ('Muziki na Sanaa', 'music'),
    ('Wanyama', 'paw-print'),
    ('Sanaa na Kubuni', 'palette'),
    ('Bidhaa Nyingine', 'package'),
]
for name, icon in cats:
    cat, created = MarketplaceCategory.objects.get_or_create(name=name, defaults={'icon': icon})
    print(f"{'Created' if created else 'Exists'}: {name}")
print('Done!')
