from django.db import migrations, models


def backfill_driver_type(apps, schema_editor):
    Driver = apps.get_model('delivery', 'Driver')
    Driver.objects.filter(serves_all=True).update(driver_type='platform')
    Driver.objects.filter(serves_all=False).update(driver_type='restaurant')


class Migration(migrations.Migration):

    dependencies = [
        ('delivery', '0006_driver_home_zone'),
    ]

    operations = [
        migrations.AddField(
            model_name='driver',
            name='driver_type',
            field=models.CharField(
                max_length=20, default='restaurant', db_index=True,
                choices=(('platform', 'Platform Driver'), ('restaurant', 'Restaurant Driver')),
                help_text='platform=serves all restaurants, restaurant=tied to home_restaurant',
            ),
        ),
        migrations.RunPython(backfill_driver_type, reverse_code=migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='driver',
            name='serves_all',
        ),
    ]
