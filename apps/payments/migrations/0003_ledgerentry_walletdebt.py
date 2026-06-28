# Manually written for LedgerEntry and WalletDebt models

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0002_initial'),
        ('wallets', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='LedgerEntry',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('entry_type', models.CharField(max_length=50)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('payment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ledger_entries', to='payments.payment')),
                ('debit_wallet', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='debit_entries', to='wallets.wallet')),
                ('credit_wallet', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='credit_entries', to='wallets.wallet')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='WalletDebt',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('original_amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('remaining_balance', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('recovered_amount', models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ('status', models.CharField(default='OUTSTANDING', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('payment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='debts', to='payments.payment')),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='debts', to='wallets.wallet')),
            ],
        ),
    ]
