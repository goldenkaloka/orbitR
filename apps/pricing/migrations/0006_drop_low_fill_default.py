from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0005_drop_pricing'),
    ]

    operations = [
        migrations.AlterField(
            model_name='pricingpolicy',
            name='drop_low_fill_policy',
            field=models.CharField(choices=[('RUN', 'Run the trip anyway'), ('ROLL', 'Move orders to the next window'), ('CANCEL', 'Cancel and refund')], default='ROLL', help_text='What to do when a drop window has fewer than min_orders at cut-off', max_length=10),
        ),
    ]
