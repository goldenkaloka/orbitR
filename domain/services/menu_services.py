from django.db import transaction
from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection
from apps.restaurants.models import SectionStaff
from domain.events.bus import EventBus
from domain.events.types import FoodCreated, FoodDeleted

class MenuService:
    @staticmethod
    def create_food(owner_user, name, price, description='', category='MAIN', is_available=True, image=None, section=None):
        with transaction.atomic():
            try:
                restaurant = Restaurant.objects.get(owner=owner_user)
            except Restaurant.DoesNotExist:
                staff_section = SectionStaff.objects.filter(user=owner_user).select_related('section__restaurant').first()
                if staff_section:
                    restaurant = staff_section.section.restaurant
                else:
                    raise ValueError("You must own or work at a restaurant to create foods")

            food_kwargs = dict(
                vendor=restaurant,
                name=name,
                price=price,
                description=description,
                category=category,
                is_available=is_available,
                image=image,
            )
            if section is not None:
                food_kwargs['section'] = section
            elif not Restaurant.objects.filter(owner=owner_user).exists():
                staff_sec = SectionStaff.objects.filter(user=owner_user).select_related('section').first()
                if staff_sec:
                    food_kwargs['section'] = staff_sec.section

            food = Food.objects.create(**food_kwargs)

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
                staff_sec = SectionStaff.objects.filter(user=owner_user).values_list('section_id', flat=True)
                food = Food.objects.get(id=food_id, section_id__in=list(staff_sec))

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
