"""
Migration: Add DistanceTier model.

Allows distance-based pricing escalation — admin can define
multipliers for different distance ranges on each PricingPolicy.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='DistanceTier',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('min_km', models.FloatField(help_text='Start of distance range (inclusive)')),
                ('max_km', models.FloatField(help_text='End of distance range (exclusive). Use 999 for unlimited.')),
                ('multiplier', models.DecimalField(
                    decimal_places=2, default=1.0, max_digits=4,
                    help_text='Multiplier applied to per_km_rate for this range',
                )),
                ('policy', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='distance_tiers',
                    to='pricing.pricingpolicy',
                )),
            ],
            options={
                'ordering': ['policy', 'min_km'],
                'unique_together': {('policy', 'min_km')},
                'verbose_name': 'Distance Tier',
                'verbose_name_plural': 'Distance Tiers',
            },
        ),
    ]
