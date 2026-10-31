from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0006_drop_low_fill_default'),
    ]

    operations = [
        migrations.AddField(
            model_name='pricingpolicy',
            name='min_delivery_margin',
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal('150.00'),
                help_text='Minimum profit kept per delivery on top of the rider guarantee',
                max_digits=10,
            ),
        ),
    ]
