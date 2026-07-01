from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('orders', '0005_order_fulfillment_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderitem',
            name='cancelled_quantity',
            field=models.PositiveIntegerField(default=0, help_text='Quantity cancelled by vendor'),
        ),
    ]
