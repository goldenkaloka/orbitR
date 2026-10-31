from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0004_alter_pricingpolicy_driver_min_guarantee'),
    ]

    operations = [
        migrations.AddField(
            model_name='pricingpolicy',
            name='drop_trip_payout',
            field=models.DecimalField(decimal_places=2, default=3000.0, help_text='Flat payout to the rider for one scheduled-drop trip', max_digits=10),
        ),
        migrations.AddField(
            model_name='pricingpolicy',
            name='drop_low_fill_policy',
            field=models.CharField(choices=[('RUN', 'Run the trip anyway'), ('ROLL', 'Move orders to the next window'), ('CANCEL', 'Cancel and refund')], default='RUN', help_text='What to do when a drop window has fewer than min_orders at cut-off', max_length=10),
        ),
    ]
