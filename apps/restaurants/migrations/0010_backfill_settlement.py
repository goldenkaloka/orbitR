from django.db import migrations
from django.db.models import Q


def forwards(apps, schema_editor):
    Restaurant = apps.get_model('restaurants', 'Restaurant')
    queryset = Restaurant.objects.filter(
        Q(source_type='PLATFORM_MANAGED') | Q(owner__role='admin')
    ).select_related('owner')
    for restaurant in queryset:
        restaurant.settlement_mode = 'PLATFORM_PREPAID'
        if not restaurant.payout_phone and restaurant.owner_id:
            restaurant.payout_phone = restaurant.owner.phone or ''
        restaurant.save(update_fields=[
            'settlement_mode', 'payout_phone',
        ])


def backwards(apps, schema_editor):
    Restaurant = apps.get_model('restaurants', 'Restaurant')
    Restaurant.objects.filter(settlement_mode='PLATFORM_PREPAID').update(
        settlement_mode='VENDOR_PAYOUT',
    )


class Migration(migrations.Migration):
    dependencies = [
        ('restaurants', '0009_restaurant_payout_account_name_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
