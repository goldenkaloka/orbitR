from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0003_pg_trgm_extension'),
    ]

    operations = [
        migrations.AddField(
            model_name='food',
            name='meal_period',
            field=models.CharField(
                choices=[
                    ('BREAKFAST', 'Breakfast'),
                    ('LUNCH', 'Lunch'),
                    ('DINNER', 'Dinner'),
                    ('ALL_DAY', 'All day'),
                ],
                db_index=True,
                default='ALL_DAY',
                help_text=(
                    'Serving window this dish belongs to; ALL_DAY is available '
                    'at any time.'
                ),
                max_length=16,
            ),
        ),
    ]
