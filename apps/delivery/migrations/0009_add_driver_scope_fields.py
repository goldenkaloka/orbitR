from django.db import migrations, models
import django.db.models.deletion


def copy_assigned_by_to_home_restaurant(apps, schema_editor):
    Driver = apps.get_model('delivery', 'Driver')
    for driver in Driver.objects.iterator():
        if driver.assigned_by_id:
            driver.home_restaurant_id = driver.assigned_by_id
        else:
            driver.serves_all = True
        driver.save(update_fields=['home_restaurant_id', 'serves_all'])


def reverse_copy(apps, schema_editor):
    Driver = apps.get_model('delivery', 'Driver')
    for driver in Driver.objects.iterator():
        if driver.home_restaurant_id:
            driver.assigned_by_id = driver.home_restaurant_id
        driver.save(update_fields=['assigned_by_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('delivery', '0008_driver_last_heartbeat'),
        ('restaurants', '__first__'),
    ]

    operations = [
        migrations.AddField(
            model_name='driver',
            name='can_assign',
            field=models.BooleanField(default=False, help_text='Can assign other drivers to batches'),
        ),
        migrations.AddField(
            model_name='driver',
            name='home_restaurant',
            field=models.ForeignKey(blank=True, help_text='Home restaurant this driver is affiliated with', null=True, on_delete=django.db.models.deletion.CASCADE, to='restaurants.restaurant'),
        ),
        migrations.AddField(
            model_name='driver',
            name='managed_by',
            field=models.ForeignKey(blank=True, help_text='Supervisor driver who manages this driver', null=True, on_delete=django.db.models.deletion.SET_NULL, to='delivery.driver'),
        ),
        migrations.AddField(
            model_name='driver',
            name='max_radius_km',
            field=models.DecimalField(decimal_places=1, default=15.0, help_text='Maximum km from current location the driver will travel', max_digits=5),
        ),
        migrations.AddField(
            model_name='driver',
            name='priority',
            field=models.IntegerField(default=0, help_text='Higher priority = chosen first for dispatch, shown first in lists'),
        ),
        migrations.AddField(
            model_name='driver',
            name='serves_all',
            field=models.BooleanField(default=False, help_text='Platform driver — can serve orders from any restaurant'),
        ),
        migrations.RunPython(copy_assigned_by_to_home_restaurant, reverse_copy),
        migrations.RemoveField(
            model_name='driver',
            name='assigned_by',
        ),
    ]
