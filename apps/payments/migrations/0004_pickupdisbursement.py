import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Adds PickupDisbursement — the real mobile-money payout made to a restaurant
    at the moment the driver collects the food.

    Order.pickup_paid_amount lives in the orders app migration
    (0008_order_pickup_paid_amount); adding it here would have been applied to
    this app's own 'order' model, which does not exist.
    """

    dependencies = [
        ('payments', '0003_ledgerentry_walletdebt'),
        ('delivery', '0009_deliveryassignment_assignment_source'),
        ('orders', '0007_order_cancellation_metadata'),
        ('restaurants', '0008_restaurant_dispatch_mode'),
    ]

    operations = [
        migrations.CreateModel(
            name='PickupDisbursement',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10)),
                ('account_number', models.CharField(help_text='Restaurant Lipa Namba / phone the money was sent to', max_length=32)),
                ('network', models.CharField(blank=True, default='', help_text='Declared mobile-money network. Informational — the gateway routes by MSISDN prefix, not by this value.', max_length=32)),
                ('status', models.CharField(choices=[('PENDING', 'Pending'), ('SUCCESS', 'Paid'), ('FAILED', 'Failed'), ('REVERSED', 'Reversed')], db_index=True, default='PENDING', max_length=20)),
                ('gateway_reference', models.CharField(blank=True, default='', max_length=255)),
                ('failure_reason', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('settled_at', models.DateTimeField(blank=True, null=True)),
                ('batch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='pickup_disbursements', to='orders.deliverybatch')),
                ('driver', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='pickup_disbursements', to='delivery.driver')),
                ('order', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='pickup_disbursement', to='orders.order')),
                ('restaurant', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='pickup_disbursements', to='restaurants.restaurant')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]