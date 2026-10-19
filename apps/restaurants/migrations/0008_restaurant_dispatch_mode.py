from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0007_restaurant_platform_managed'),
    ]

    operations = [
        migrations.AddField(
            model_name='restaurant',
            name='dispatch_mode',
            field=models.CharField(
                choices=[
                    ('admin', 'Admin dispatch'),
                    ('restaurant', 'Restaurant dispatch'),
                ],
                db_index=True,
                default='admin',
                help_text="Who allocates drivers for this restaurant's batches.",
                max_length=20,
            ),
        ),
    ]