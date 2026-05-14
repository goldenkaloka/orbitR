from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ('products', '0003_food_category'),
    ]

    operations = [
        migrations.RunSQL(
            sql='CREATE INDEX food_name_trgm_idx ON products_food USING gin (name gin_trgm_ops);',
            reverse_sql='DROP INDEX food_name_trgm_idx;',
        ),
    ]
