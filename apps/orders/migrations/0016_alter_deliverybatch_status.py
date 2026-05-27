# Generated manually — migrates DeliveryBatch status values to the Phase 2
# dispatch-only model: CREATED, OFFERED, EXPIRED, WAITING_TEMP_DRIVER,
# DISPATCHED, CANCELLED.
#
# Old values ASSIGNED → DISPATCHED, PICKED → DISPATCHED, COMPLETED → DISPATCHED.
# No schema change — only choices on the Python model are updated.

from django.db import migrations


def migrate_batch_statuses(apps, schema_editor):
    DeliveryBatch = apps.get_model('orders', 'DeliveryBatch')
    DeliveryBatch.objects.filter(status__in=['ASSIGNED', 'PICKED', 'COMPLETED']).update(
        status='DISPATCHED'
    )


def reverse_batch_statuses(apps, schema_editor):
    DeliveryBatch = apps.get_model('orders', 'DeliveryBatch')
    DeliveryBatch.objects.filter(status='DISPATCHED').update(status='ASSIGNED')


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0015_orderpool_batch_order_is_pool_order'),
    ]

    operations = [
        migrations.RunPython(migrate_batch_statuses, reverse_batch_statuses),
    ]
