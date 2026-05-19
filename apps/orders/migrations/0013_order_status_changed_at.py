from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0012_batchingstrategy_deliverybatch_batch_type_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='status_changed_at',
            field=models.DateTimeField(auto_now_add=True, db_index=True),
        ),
    ]