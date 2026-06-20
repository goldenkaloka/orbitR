from apps.marketplace.models import MarketplaceCategory
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Seeds marketplace categories with Swahili names'

    def handle(self, *args, **kwargs):
        categories = [
            ('Elektroniki', 'cpu'),
            ('Mavazi', 'shirt'),
            ('Vitabu', 'book-open'),
            ('Samani', 'sofa'),
            ('Vyombo vya Nyumbani', 'home'),
            ('Chakula na Vinywaji', 'utensils-crossed'),
            ('Simu na Kompyuta', 'smartphone'),
            ('Vifaa vya Michezo', 'dumbbell'),
            ('Vito na Mapambo', 'gem'),
            ('Vifaa vya Mboga', 'leaf'),
            ('Vifaa vya Nyumbani', 'wrench'),
            ('Vifaa vya Watoto', 'baby'),
            ('Vifaa vya Afya', 'pill'),
            ('Vifaa vya Usafiri', 'car'),
            ('Vifaa vya Muziki', 'music'),
            ('Vifaa vya Kutosha', 'package'),
            ('Vifaa vya Kazi', 'laptop'),
            ('Vifaa vya Shule', 'backpack'),
            ('Vifaa vya Kupikia', 'cake'),
            ('Vifaa vya Wanyama', 'paw-print'),
            ('Vifaa vya Zahanati', 'store'),
            ('Vifaa vya Kupamba', 'palette'),
            ('Vifaa vya Usafiri', 'plane'),
            ('Vifaa vya Kukodisha', 'truck'),
        ]

        self.stdout.write(self.style.SUCCESS('Jaza kategoria kwa Kiswahili...'))

        for name, icon in categories:
            cat, created = MarketplaceCategory.objects.get_or_create(
                name=name,
                defaults={'icon': icon}
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f" imeundwa: {name}"))
            else:
                self.stdout.write(self.style.WARNING(f" ipo tayari: {name}"))

        self.stdout.write(self.style.SUCCESS('Kategoria zimejazwa kikamilifu!'))
