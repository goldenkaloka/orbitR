from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0004_deliveryzone_operational_zone_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="restaurant",
            name="h3_index",
            field=models.CharField(max_length=20, null=True, blank=True, db_index=True),
        ),
        migrations.AddIndex(
            model_name="restaurant",
            index=models.Index(
                fields=["is_active", "category", "h3_index"],
                name="resto_list_geo_idx",
            ),
        ),
    ]

