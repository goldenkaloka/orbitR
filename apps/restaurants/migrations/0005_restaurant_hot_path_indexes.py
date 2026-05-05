from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("restaurants", "0004_deliveryzone_operational_zone_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="restaurant",
            index=models.Index(
                fields=["is_active", "category", "h3_index"],
                name="resto_list_geo_idx",
            ),
        ),
    ]

