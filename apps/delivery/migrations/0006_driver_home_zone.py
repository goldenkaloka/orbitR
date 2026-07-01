from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('delivery', '0005_alter_driver_max_active_orders'),
        ('geo', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='driver',
            name='home_zone',
            field=models.ForeignKey(
                blank=True,
                help_text='Base operational zone used to identify the driver when offline',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='drivers',
                to='geo.operationalzone',
            ),
        ),
    ]
