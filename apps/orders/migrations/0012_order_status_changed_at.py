from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0011_deliverybatch_zone_nullable'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='status_changed_at',
            field=models.DateTimeField(auto_now_add=True, db_index=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
    ]