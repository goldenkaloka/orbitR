# Three conditions instead of four: new, used, other.

from django.db import migrations, models


def collapse_conditions(apps, schema_editor):
    """Fold the old like-new/good/fair grades into USED.

    The extra grades split the same second-hand listings four ways without
    helping buyers decide, and sellers picked them inconsistently.
    """
    MarketplaceProduct = apps.get_model('marketplace', 'MarketplaceProduct')
    MarketplaceProduct.objects.filter(
        condition__in=['LIKE_NEW', 'GOOD', 'FAIR', 'POOR']
    ).update(condition='USED')


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0005_seed_posting_tokens'),
    ]

    operations = [
        migrations.RunPython(collapse_conditions, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='marketplaceproduct',
            name='condition',
            field=models.CharField(
                choices=[('NEW', 'New'), ('USED', 'Used'), ('OTHER', 'Other')],
                default='USED',
                max_length=20,
            ),
        ),
    ]
