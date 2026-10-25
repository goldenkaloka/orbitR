from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0012_order_scheduled_for_release_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='deliverybatch',
            name='preorder_slot_start',
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text='Eating-time slot this batch was pre-grouped for.',
                null=True,
            ),
        ),
    ]
