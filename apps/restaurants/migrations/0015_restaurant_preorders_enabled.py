from django.db import migrations, models


def enable_configured_restaurants(apps, schema_editor):
    """Keep pre-orders on for kitchens that already configured a schedule."""
    Restaurant = apps.get_model('restaurants', 'Restaurant')
    MealPeriod = apps.get_model('restaurants', 'MealPeriod')
    configured = MealPeriod.objects.values_list('restaurant_id', flat=True).distinct()
    Restaurant.objects.filter(id__in=list(configured)).update(preorders_enabled=True)


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0014_sync_verified_partner'),
    ]

    operations = [
        migrations.AddField(
            model_name='restaurant',
            name='preorders_enabled',
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    'Whether this restaurant takes pre-orders. Off by default; '
                    'turning it on with no meal periods uses the platform windows.'
                ),
            ),
        ),
        migrations.RunPython(
            enable_configured_restaurants,
            migrations.RunPython.noop,
        ),
    ]
