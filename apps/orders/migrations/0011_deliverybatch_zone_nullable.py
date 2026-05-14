"""
Migration: Remove zone FK from Order and DeliveryBatch.

This MUST run BEFORE restaurants.0009_delete_deliveryzone because
Django cannot delete a model while other models still have FKs pointing to it.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0010_order_service_fee_orderpool_final_order_and_more'),
        ('restaurants', '0008_radius_based_delivery_zones'),  # NOT 0009!
    ]

    operations = [
        # Remove zone FK from Order
        migrations.RemoveField(
            model_name='order',
            name='zone',
        ),

        # Remove zone FK from DeliveryBatch
        migrations.RemoveField(
            model_name='deliverybatch',
            name='zone',
        ),
    ]
