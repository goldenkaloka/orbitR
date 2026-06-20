"""
Seed default distance tiers for the active PricingPolicy.

Run: python manage.py shell < seed_distance_tiers.py
"""
import django

django.setup()

from decimal import Decimal

from apps.pricing.models import DistanceTier, PricingPolicy

policy = PricingPolicy.objects.filter(is_active=True).first()

if not policy:
    print("No active PricingPolicy found. Create one first.")
    exit(1)

# Default tiers for Dar es Salaam delivery
tiers = [
    {'min_km': 0,  'max_km': 2,   'multiplier': Decimal('1.00')},  # Short range — base rate
    {'min_km': 2,  'max_km': 5,   'multiplier': Decimal('1.30')},  # Medium — 30% more
    {'min_km': 5,  'max_km': 8,   'multiplier': Decimal('1.60')},  # Far — 60% more
    {'min_km': 8,  'max_km': 999, 'multiplier': Decimal('2.00')},  # Very far — double
]

created = 0
for tier_data in tiers:
    _, was_created = DistanceTier.objects.update_or_create(
        policy=policy,
        min_km=tier_data['min_km'],
        defaults={
            'max_km': tier_data['max_km'],
            'multiplier': tier_data['multiplier'],
        }
    )
    if was_created:
        created += 1

print(f"Distance tiers seeded: {created} created, {len(tiers) - created} updated.")
print(f"Policy: {policy.name}")
for tier in DistanceTier.objects.filter(policy=policy).order_by('min_km'):
    print(f"  {tier.min_km}-{tier.max_km}km: {tier.multiplier}x")
