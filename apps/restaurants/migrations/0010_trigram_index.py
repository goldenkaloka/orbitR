from django.db import migrations
from django.contrib.postgres.operations import TrigramExtension
from django.contrib.postgres.indexes import GinIndex

class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0009_delete_deliveryzone'),
    ]

    operations = [
        migrations.RunSQL(
            sql='CREATE INDEX restaurant_name_trgm_idx ON restaurants_restaurant USING gin (name gin_trgm_ops);',
            reverse_sql='DROP INDEX restaurant_name_trgm_idx;',
        ),
    ]
