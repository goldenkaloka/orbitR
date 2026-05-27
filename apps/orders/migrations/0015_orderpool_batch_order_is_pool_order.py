"""
Migration 0015: Pool ↔ Batch unified system

- OrderPool.batch  — FK to DeliveryBatch so a pool knows its batch
                     (SET_NULL so cancelling a batch doesn't cascade-delete the pool)
- Order.is_pool_order — boolean flag so the batching pipeline can skip
                        H3 re-clustering for pool orders (they already share
                        a delivery point by design)
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0014_deliverybatch_waiting_since_and_more'),
    ]

    operations = [
        # Pool knows which batch is delivering its consolidated order
        migrations.AddField(
            model_name='orderpool',
            name='batch',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='source_pools',
                to='orders.deliverybatch',
                help_text='The delivery batch created when this pool was finalised.',
            ),
        ),
        # Flag so BatchPipeline can fast-path pool orders
        migrations.AddField(
            model_name='order',
            name='is_pool_order',
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text='True when this order was created by finalising an OrderPool.',
            ),
        ),
    ]
