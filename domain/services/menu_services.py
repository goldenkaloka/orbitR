from django.db import transaction
from apps.products.models import Food
from apps.restaurants.models import Restaurant
from domain.events.bus import EventBus
from domain.events.types import FoodCreated, FoodDeleted

class MenuService:
    @staticmethod
    def create_food(owner_user, name, price, description='', category='MAIN', is_available=True, image=None):
        with transaction.atomic():
            try:
                restaurant = Restaurant.objects.get(owner=owner_user)
            except Restaurant.DoesNotExist:
                raise ValueError("You must own a restaurant to create foods")

            food = Food.objects.create(
                vendor=restaurant,
                name=name,
                price=price,
                description=description,
                category=category,
                is_available=is_available,
                image=image
            )

            EventBus.publish(FoodCreated(
                aggregate_id=food.id,
                payload={
                    'food_id': str(food.id),
                    'restaurant_id': str(restaurant.id),
                    'name': food.name
                }
            ))
            return food

    @staticmethod
    def delete_food(owner_user, food_id):
        with transaction.atomic():
            try:
                food = Food.objects.get(id=food_id, vendor__owner=owner_user)
            except Food.DoesNotExist:
                raise ValueError("Food item not found or you do not have permission to delete it")

            food_name = food.name
            restaurant_id = food.vendor.id
            food.delete()

            EventBus.publish(FoodDeleted(
                aggregate_id=food_id,
                payload={
                    'food_id': str(food_id),
                    'restaurant_id': str(restaurant_id),
                    'name': food_name
                }
            ))
            return True
