from django.db import migrations, models


def normalise_legacy_popular(apps, schema_editor):
    """
    `POPULAR` used to be a category. It is a promotion, not a dish type, so
    move those rows to Main and flag them instead. Old clients can still send
    `POPULAR`; the field stays in CATEGORY_CHOICES for that reason.
    """
    Food = apps.get_model('products', 'Food')
    Food.objects.filter(category='POPULAR').update(
        category='MAIN',
        is_popular=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0004_food_meal_period'),
    ]

    operations = [
        migrations.AddField(
            model_name='food',
            name='is_popular',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.RunPython(
            normalise_legacy_popular,
            migrations.RunPython.noop,
        ),
    ]
