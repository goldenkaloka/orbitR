import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.products.models import Product
from apps.delivery.models import Driver

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

    # 3. Create Restaurant
    res, _ = Restaurant.objects.get_or_create(
        name='Mwanachuo Grill', 
        owner=owner,
        defaults={'location': 'Main Campus', 'is_active': True}
    )
    print(f"Restaurant Created ID: {res.id}")

    # 4. Create Product
    prod, _ = Product.objects.get_or_create(
        name='Special Burger',
        vendor=res,
        defaults={'price': 5000.00, 'is_available': True}
    )
    print(f"Product Created ID: {prod.id}")

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

if __name__ == '__main__':
    setup_data()
