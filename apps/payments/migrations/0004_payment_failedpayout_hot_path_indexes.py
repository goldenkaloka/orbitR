from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0003_failedpayout_error_code_payment_marketplace_product_and_more"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="payment",
            index=models.Index(
                fields=["status", "created_at"],
                name="payment_status_ct_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="failedpayout",
            index=models.Index(
                fields=["retry_count", "created_at"],
                name="failedpayout_retry_idx",
            ),
        ),
    ]

