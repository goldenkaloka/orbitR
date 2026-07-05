from django.db import migrations


def create_default_sections(apps, schema_editor):
    Restaurant = apps.get_model('restaurants', 'Restaurant')
    RestaurantSection = apps.get_model('restaurants', 'RestaurantSection')

    for restaurant in Restaurant.objects.all().iterator():
        if RestaurantSection.objects.filter(restaurant=restaurant).exists():
            continue
        RestaurantSection.objects.create(
            restaurant=restaurant,
            name='Main Kitchen',
            specialization='General',
            description='Default preparation area',
            sort_order=0,
            prep_time=restaurant.average_prep_time or 20,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0004_restaurant_logo'),
    ]

    operations = [
        migrations.RunPython(create_default_sections, migrations.RunPython.noop),
    ]
