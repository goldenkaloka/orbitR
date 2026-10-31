from django.db import migrations


def sync_verified_partner(apps, schema_editor):
    """Make is_verified_partner agree with source_type on existing rows.

    The flag is now derived in Restaurant.save(); rows written before that
    (default True on creation) may disagree for platform-managed listings.
    """
    Restaurant = apps.get_model('restaurants', 'Restaurant')
    Restaurant.objects.filter(source_type='PLATFORM_MANAGED').update(
        is_verified_partner=False,
    )
    Restaurant.objects.filter(source_type='PARTNER').update(
        is_verified_partner=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('restaurants', '0013_mealperiod_preorder_cutoff_time'),
    ]

    operations = [
        migrations.RunPython(sync_verified_partner, migrations.RunPython.noop),
    ]
