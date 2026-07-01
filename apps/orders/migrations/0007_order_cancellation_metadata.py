from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0006_orderitem_cancelled_quantity'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='affects_restaurant_reputation',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='order',
            name='cancellation_category',
            field=models.CharField(blank=True, choices=[('customer_changed_mind', 'Customer changed mind'), ('payment_timeout', 'Payment timeout'), ('vendor_rejected', 'Restaurant rejected'), ('item_unavailable', 'Item unavailable'), ('restaurant_delay', 'Restaurant delay'), ('driver_cancelled', 'Driver cancelled'), ('driver_no_show', 'Driver no-show'), ('system_timeout', 'System timeout'), ('admin_cancelled', 'Admin cancelled'), ('pool_cancelled', 'Pool cancelled'), ('other', 'Other')], db_index=True, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='cancellation_fault',
            field=models.CharField(blank=True, choices=[('CUSTOMER', 'Customer fault'), ('VENDOR', 'Restaurant fault'), ('DRIVER', 'Driver fault'), ('PLATFORM', 'Platform fault'), ('SYSTEM', 'System/automatic'), ('NONE', 'No fault')], db_index=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='cancellation_reason',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='cancelled_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='cancelled_by',
            field=models.CharField(blank=True, choices=[('CUSTOMER', 'Customer'), ('VENDOR', 'Restaurant'), ('DRIVER', 'Driver'), ('ADMIN', 'Admin'), ('SYSTEM', 'System')], db_index=True, max_length=20, null=True),
        ),
        migrations.AddIndex(
            model_name='order',
            index=models.Index(fields=['restaurant', 'cancellation_fault', 'cancelled_at'], name='order_cancel_fault_idx'),
        ),
    ]
