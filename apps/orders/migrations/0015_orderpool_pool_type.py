from django.db import migrations, models


def backfill_pool_type(apps, schema_editor):
    OrderPool = apps.get_model('orders', 'OrderPool')
    OrderPool.objects.filter(scheduled_for__isnull=True).update(pool_type='IMMEDIATE')
    OrderPool.objects.filter(scheduled_for__isnull=False).update(pool_type='PREORDER')


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0014_orderpool_scheduled_for'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderpool',
            name='pool_type',
            field=models.CharField(
                choices=[
                    ('IMMEDIATE', 'Deliver as soon as ready'),
                    ('PREORDER', 'Deliver at the booked eating time'),
                ],
                db_index=True,
                default='IMMEDIATE',
                max_length=16,
            ),
        ),
        migrations.RunPython(backfill_pool_type, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='orderpool',
            constraint=models.CheckConstraint(
                check=(
                    models.Q(pool_type='PREORDER', scheduled_for__isnull=False)
                    | models.Q(pool_type='IMMEDIATE', scheduled_for__isnull=True)
                ),
                name='pool_type_matches_schedule',
            ),
        ),
    ]
