from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0018_drop_points'),
    ]

    operations = [
        migrations.AlterField(
            model_name='droppoint',
            name='min_orders',
            field=models.PositiveIntegerField(default=4, help_text='Orders needed for the trip to pay for itself'),
        ),
        migrations.AlterField(
            model_name='droppoint',
            name='max_orders',
            field=models.PositiveIntegerField(default=5, help_text='Orders one rider can carry on a single trip'),
        ),
    ]
