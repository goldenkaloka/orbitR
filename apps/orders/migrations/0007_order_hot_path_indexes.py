from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0006_alter_orderitem_food"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                fields=["status", "is_paid", "driver", "-created_at"],
                name="order_job_feed_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                fields=["h3_index", "status", "is_paid", "driver"],
                name="order_h3_status_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="order",
            index=models.Index(
                fields=["restaurant", "is_paid", "created_at"],
                name="order_vendor_day_idx",
            ),
        ),
    ]

