import os
import django
import uuid
import random
from django.core.files import File

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.products.models import Food

def seed():
    user = User.objects.first()
    if not user:
        print("No user found. Please create a superuser first.")
        return

    restaurant_names = ["Lulu's Kitchen", "Karibu Grill", "Safari Bites", "Tanzania Taste", "Dar Deli"]
    categories = ["Swahili", "Grill", "Traditional", "Seafood", "Fast Food"]
    
    restaurant_image_path = "media/restaurants/seed.png"
    food_image_path = "media/products/seed.png"

    for i in range(5):
        restaurant = Restaurant.objects.create(
            owner=user,
            name=restaurant_names[i],
            description=f"Authentic {categories[i]} food in the heart of the city.",
            location=f"Street {i+1}, Dar es Salaam",
            category=categories[i],
            price_range="TSH 5k–15k",
            rating=random.uniform(4.0, 5.0)
        )
        
        # Add image if it exists
        if os.path.exists(restaurant_image_path):
            try:
                with open(restaurant_image_path, 'rb') as f:
                    restaurant.image.save(f'res_{i}.png', File(f), save=True)
            except Exception as e:
                print(f"Could not save restaurant image: {e}")

        print(f"Created Restaurant: {restaurant.name}")

        for j in range(10):
            food_names = [
                f"{categories[i]} Special", "Mshikaki", "Pilau", "Ugali Nyama", "Samaki",
                "Chips Mayai", "Nyama Choma", "Wali Maharage", "Ndizi", "Samosa"
            ]
            food = Food.objects.create(
                vendor=restaurant,
                name=f"{restaurant.name} {food_names[j % len(food_names)]} {j+1}",
                description=f"A delicious {categories[i]} specialty made with fresh ingredients.",
                price=random.randint(5000, 25000),
                is_available=True
            )
            
            if os.path.exists(food_image_path):
                try:
                    with open(food_image_path, 'rb') as f:
                        food.image.save(f'food_{i}_{j}.png', File(f), save=True)
                except Exception as e:
                    print(f"Could not save food image: {e}")
            
            print(f"  Added Food: {food.name}")

if __name__ == "__main__":
    seed()
