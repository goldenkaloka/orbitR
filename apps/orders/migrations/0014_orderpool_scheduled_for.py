from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0013_deliverybatch_preorder_slot_start'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderpool',
            name='scheduled_for',
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text='Eating time for a group pre-order; null for immediate buckets.',
                null=True,
            ),
        ),
    ]
