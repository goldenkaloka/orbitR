from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


def clamp_negative_balances(apps, schema_editor):
    """Existing wallets may hold negative balances from past bugs.

    The check constraints added below would abort the migration on those rows,
    so normalise them first. Zero is the only safe value: a negative balance is
    already invalid and keeping the old number would block every future save.
    """
    Wallet = apps.get_model('wallets', 'Wallet')
    Wallet.objects.filter(balance__lt=0).update(balance=0)
    Wallet.objects.filter(pending_balance__lt=0).update(pending_balance=0)


class Migration(migrations.Migration):

    dependencies = [
        ('wallets', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='WalletPayout',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('fee', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('net_amount', models.DecimalField(decimal_places=2, max_digits=12)),
                ('provider', models.CharField(blank=True, default='', max_length=32)),
                ('destination_phone', models.CharField(max_length=20)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('PROCESSING', 'Processing'), ('SUCCESS', 'Success'), ('FAILED', 'Failed'), ('UNKNOWN', 'Unknown')], db_index=True, default='PENDING', max_length=20)),
                ('reference', models.CharField(max_length=64, unique=True)),
                ('provider_reference', models.CharField(blank=True, default='', max_length=255)),
                ('failure_reason', models.TextField(blank=True, default='')),
                ('idempotency_key', models.CharField(blank=True, max_length=128, null=True, unique=True)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='wallet_payouts', to=settings.AUTH_USER_MODEL)),
                ('wallet', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payouts', to='wallets.wallet')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='transaction_type',
            field=models.CharField(choices=[('PAYMENT', 'Payment'), ('REFUND', 'Refund'), ('PAYOUT', 'Payout'), ('FEE', 'Fee'), ('EARNING', 'Earning'), ('CLAWBACK', 'Clawback'), ('DEBT_RECOVERY', 'Debt recovery'), ('ADJUSTMENT', 'Adjustment'), ('REVENUE', 'Platform revenue'), ('REVERSAL', 'Reversal')], db_index=True, default='ADJUSTMENT', max_length=20),
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='status',
            field=models.CharField(choices=[('POSTED', 'Posted'), ('REVERSED', 'Reversed')], db_index=True, default='POSTED', max_length=10),
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='reference',
            field=models.CharField(blank=True, max_length=96, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='idempotency_key',
            field=models.CharField(blank=True, max_length=128, null=True, unique=True),
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='balance_after',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True),
        ),
        migrations.AddField(
            model_name='wallettransaction',
            name='payout',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transactions', to='wallets.walletpayout'),
        ),
        migrations.RunPython(clamp_negative_balances, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='wallet',
            constraint=models.CheckConstraint(check=models.Q(('balance__gte', 0)), name='wallet_balance_non_negative'),
        ),
        migrations.AddConstraint(
            model_name='wallet',
            constraint=models.CheckConstraint(check=models.Q(('pending_balance__gte', 0)), name='wallet_pending_non_negative'),
        ),
        migrations.AddConstraint(
            model_name='wallettransaction',
            constraint=models.CheckConstraint(check=models.Q(('amount__gt', 0)), name='wallet_transaction_amount_positive'),
        ),
        migrations.AddConstraint(
            model_name='walletpayout',
            constraint=models.CheckConstraint(check=models.Q(('status__in', ['PENDING', 'PROCESSING', 'SUCCESS', 'FAILED', 'UNKNOWN'])), name='wallet_payout_status_valid'),
        ),
        migrations.AddIndex(
            model_name='wallettransaction',
            index=models.Index(fields=['wallet', '-created_at'], name='wallet_txn_recent_idx'),
        ),
    ]
