from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('delivery', '0007_remove_driver_latitude_remove_driver_longitude'),
    ]

    operations = [
        migrations.AddField(
            model_name='driver',
            name='last_heartbeat',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]