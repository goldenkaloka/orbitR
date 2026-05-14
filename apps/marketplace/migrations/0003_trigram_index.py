from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0002_initial'),
    ]

    operations = [
        migrations.RunSQL(
            sql='CREATE INDEX marketplace_product_name_trgm_idx ON marketplace_marketplaceproduct USING gin (name gin_trgm_ops);',
            reverse_sql='DROP INDEX marketplace_product_name_trgm_idx;',
        ),
    ]
