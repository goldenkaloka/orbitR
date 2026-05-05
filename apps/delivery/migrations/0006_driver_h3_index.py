from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("delivery", "0005_driver_latitude_driver_longitude"),
    ]

    operations = [
        migrations.AddField(
            model_name="driver",
            name="h3_index",
            field=models.CharField(
                blank=True, db_index=True, max_length=20, null=True
            ),
        ),
    ]
