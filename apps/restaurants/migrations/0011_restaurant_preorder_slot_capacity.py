from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0010_backfill_settlement'),
    ]

    operations = [
        migrations.AddField(
            model_name='restaurant',
            name='preorder_slot_capacity',
            field=models.PositiveIntegerField(
                blank=True,
                help_text='Pre-orders accepted per slot; blank derives it from kitchen throughput.',
                null=True,
            ),
        ),
    ]
