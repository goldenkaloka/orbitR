from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0007_failedrefund'),
    ]

    operations = [
        migrations.AddField(
            model_name='ledgerentry',
            name='gateway_fee',
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=10,
                help_text="Payout fee the gateway charged, already netted out of amount",
            ),
        ),
        migrations.AddField(
            model_name='pickupdisbursement',
            name='gateway_fee',
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=10,
                help_text="Gateway fee for this payout (TZS)",
            ),
        ),
        migrations.AddField(
            model_name='pickupdisbursement',
            name='gateway_fee_source',
            field=models.CharField(
                blank=True, default='', max_length=16,
                help_text="GATEWAY when the provider reported the fee, ESTIMATE otherwise",
            ),
        ),
    ]
