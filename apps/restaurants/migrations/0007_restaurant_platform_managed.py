from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('restaurants', '0006_alter_restaurant_commission_rate'),
    ]

    operations = [
        migrations.AddField(
            model_name='restaurant',
            name='source_type',
            field=models.CharField(
                choices=[
                    ('PARTNER', 'Verified partner'),
                    ('PLATFORM_MANAGED', 'Platform-managed concierge'),
                ],
                db_index=True,
                default='PARTNER',
                help_text='Partner restaurants are operated by the vendor; platform-managed listings are fulfilled by OrbitR ops.',
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='is_verified_partner',
            field=models.BooleanField(
                db_index=True,
                default=True,
                help_text='True only when the restaurant directly manages and confirms its own listing.',
            ),
        ),
        migrations.AddField(
            model_name='restaurant',
            name='public_disclaimer',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Optional customer-facing note for platform-managed listings.',
                max_length=255,
            ),
        ),
    ]
