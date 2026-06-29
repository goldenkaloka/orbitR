from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0004_batchsectionstatus_estimated_ready_at_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='fulfillment_type',
            field=models.CharField(
                choices=[('DELIVERY', 'Delivery'), ('PICKUP', 'Pickup')],
                db_index=True,
                default='DELIVERY',
                max_length=20,
            ),
        ),
    ]
