from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0008_gateway_fees'),
    ]

    operations = [
        migrations.AddField(
            model_name='payment',
            name='gateway_fee',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                help_text='Gateway fee charged when collecting this payment (TZS)',
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name='payment',
            name='gateway_fee_source',
            field=models.CharField(
                blank=True,
                default='',
                help_text='GATEWAY when the provider reported the fee, ESTIMATE otherwise',
                max_length=16,
            ),
        ),
    ]
