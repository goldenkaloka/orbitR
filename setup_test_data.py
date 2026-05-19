import os
import django
from django.contrib.gis.geos import Point

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.products.models import Food
from apps.delivery.models import Driver
from apps.pricing.models import PricingPolicy, DistanceTier
from decimal import Decimal

def setup_data():
    # 1. Create Admin
    if not User.objects.filter(username='admin').exists():
        User.objects.create_superuser('admin', 'admin@example.com', 'password123', phone='255700000000')
        print("Admin created: admin / password123")

    # 2. Create Restaurant Owner
    owner, _ = User.objects.get_or_create(
        username='vendor1', 
        defaults={'role': 'vendor', 'phone': '255711111111'}
    )
    owner.set_password('password123')
    owner.save()

    # 3. Create Restaurant (located at UDSM campus inside the active operational zone)
    udsm_coords = Point(39.2062, -6.7711) # Longitude, Latitude
    res, created = Restaurant.objects.get_or_create(
        name='Mwanachuo Grill', 
        owner=owner,
        defaults={
            'location': 'Main Campus',
            'is_active': True,
            'geo_location': udsm_coords
        }
    )
    if not created and not res.geo_location:
        res.geo_location = udsm_coords
        res.save()
        print(f"Updated existing Restaurant '{res.name}' with geolocation.")
    else:
        print(f"Restaurant Created ID: {res.id}")

    # 4. Create Food (replacing incorrect Product class reference)
    food, _ = Food.objects.get_or_create(
        name='Special Burger',
        vendor=res,
        defaults={'price': 5000.00, 'is_available': True}
    )
    print(f"Food Created ID: {food.id}")

    # 5. Create Driver
    d_user, _ = User.objects.get_or_create(
        username='driver1',
        defaults={'role': 'driver', 'phone': '255722222222'}
    )
    d_user.set_password('password123')
    d_user.save()
    
    drv, _ = Driver.objects.get_or_create(
        user=d_user,
        defaults={'status': 'ONLINE'}
    )
    print(f"Driver Created ID: {drv.id}")

    # 6. Create and Activate Default Pricing Policy
    policy, p_created = PricingPolicy.objects.get_or_create(
        name='Default Policy',
        defaults={
            'is_active': True,
            'base_fee': Decimal('1000.00'),
            'per_km_rate': Decimal('500.00'),
            'service_fee': Decimal('300.00'),
            'fuel_price_reference': Decimal('3000.00')
        }
    )
    if not p_created and not policy.is_active:
        policy.is_active = True
        policy.save()
        print("Activated existing 'Default Policy'.")
    elif p_created:
        print("Created and activated 'Default Policy'.")

    # Seed Distance Tiers for the active PricingPolicy
    tiers = [
        {'min_km': 0,  'max_km': 2,   'multiplier': Decimal('1.00')},
        {'min_km': 2,  'max_km': 5,   'multiplier': Decimal('1.30')},
        {'min_km': 5,  'max_km': 8,   'multiplier': Decimal('1.60')},
        {'min_km': 8,  'max_km': 999, 'multiplier': Decimal('2.00')},
    ]
    
    tiers_created = 0
    for tier_data in tiers:
        _, t_created = DistanceTier.objects.get_or_create(
            policy=policy,
            min_km=tier_data['min_km'],
            defaults={
                'max_km': tier_data['max_km'],
                'multiplier': tier_data['multiplier']
            }
        )
        if t_created:
            tiers_created += 1
            
    if tiers_created > 0:
        print(f"Seeded {tiers_created} distance tiers for Default Policy.")


if __name__ == '__main__':
    setup_data()
