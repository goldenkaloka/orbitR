from django.db import migrations, models


STATUS_CHOICES = [
    ('PENDING_PAYMENT', 'Pending Payment'),
    ('SCHEDULED', 'Scheduled'),
    ('CREATED', 'Created'),
    ('AWAITING_CONFIRMATION', 'Awaiting Confirmation'),
    ('CONFIRMED', 'Confirmed'),
    ('PREPARING', 'Preparing'),
    ('READY', 'Ready'),
    ('ASSIGNED', 'Assigned'),
    ('PICKED', 'Picked'),
    ('DELIVERED', 'Delivered'),
    ('CANCELLED', 'Cancelled'),
    ('DISPUTED', 'Disputed'),
    ('RESOLVED', 'Resolved'),
    ('REFUNDED', 'Refunded'),
    ('EXPIRED', 'Expired'),
]


def normalize_manual_assignment(apps, schema_editor):
    Order = apps.get_model('orders', 'Order')
    Order.objects.filter(status='PENDING_MANUAL_ASSIGNMENT').update(status='CONFIRMED')


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0019_drop_capacity_defaults'),
    ]

    operations = [
        migrations.RunPython(normalize_manual_assignment, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name='order',
            name='order_status_valid',
        ),
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=STATUS_CHOICES,
                db_index=True,
                default='PENDING_PAYMENT',
                max_length=50,
            ),
        ),
        migrations.AddConstraint(
            model_name='order',
            constraint=models.CheckConstraint(
                check=models.Q(('status__in', [value for value, _ in STATUS_CHOICES])),
                name='order_status_valid',
            ),
        ),
        migrations.DeleteModel(
            name='PoolItem',
        ),
    ]
