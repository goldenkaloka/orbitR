"""
Migration: Delete DeliveryZone model.

This runs AFTER orders.0011 which removes all FKs pointing to DeliveryZone.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0008_radius_based_delivery_zones'),
        ('orders', '0011_deliverybatch_zone_nullable'),  # FKs removed first
    ]

    operations = [
        migrations.DeleteModel(
            name='DeliveryZone',
        ),
    ]
