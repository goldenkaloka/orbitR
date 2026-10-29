from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallets', '0002_wallet_hardening'),
    ]

    operations = [
        migrations.AddField(
            model_name='walletpayout',
            name='gateway_fee',
            field=models.DecimalField(
                decimal_places=2, default=0, max_digits=12,
                help_text="Fee the payout gateway charged (TZS)",
            ),
        ),
        migrations.AddField(
            model_name='walletpayout',
            name='gateway_fee_source',
            field=models.CharField(
                blank=True, default='', max_length=16,
                help_text="GATEWAY when the provider reported the fee, ESTIMATE otherwise",
            ),
        ),
    ]
