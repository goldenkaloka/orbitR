from django.db import migrations, models

STATUSES = [
    'PENDING_PAYMENT',
    'PENDING_MANUAL_ASSIGNMENT',
    'SCHEDULED',
    'CREATED',
    'AWAITING_CONFIRMATION',
    'CONFIRMED',
    'PREPARING',
    'READY',
    'ASSIGNED',
    'PICKED',
    'DELIVERED',
    'CANCELLED',
    'DISPUTED',
    'RESOLVED',
    'REFUNDED',
    'EXPIRED',
]


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0011_remove_order_order_status_valid_and_more'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='order',
            name='order_status_valid',
        ),
        migrations.AddField(
            model_name='order',
            name='scheduled_for',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='release_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name='order',
            name='status',
            field=models.CharField(
                choices=[(s, s.replace('_', ' ').title()) for s in STATUSES],
                db_index=True,
                default='PENDING_PAYMENT',
                max_length=50,
            ),
        ),
        migrations.AddConstraint(
            model_name='order',
            constraint=models.CheckConstraint(
                check=models.Q(('status__in', STATUSES)),
                name='order_status_valid',
            ),
        ),
    ]
