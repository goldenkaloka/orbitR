from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection, SectionStaff
from django.db import transaction
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
                name=name,
                price=price,
                description=description,
                category=category,
                is_available=is_available,
                image=image,
            )
            if section is not None:
                food_kwargs['section'] = section
            else:
                # If staff member, auto-assign their stall/section
                staff_sec = SectionStaff.objects.filter(user=owner_user).select_related('section').first()
                if staff_sec:
                    food_kwargs['section'] = staff_sec.section
                else:
                    # Fallback to the first section of the restaurant
                    first_section = RestaurantSection.objects.filter(restaurant=restaurant).first()
                    if not first_section:
                        raise ValueError("A section (stall) must be created before adding foods.")
                    food_kwargs['section'] = first_section

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
                food = Food.objects.get(id=food_id, section__restaurant__owner=owner_user)
            except Food.DoesNotExist:
                staff_sec = SectionStaff.objects.filter(user=owner_user).values_list('section_id', flat=True)
                if not staff_sec:
                    raise ValueError("You do not have permission to delete this food item")
                try:
                    food = Food.objects.get(id=food_id, section_id__in=list(staff_sec))
                except Food.DoesNotExist:
                    raise ValueError("Food not found or you do not have permission to delete it")

            food_name = food.name
            restaurant_id = food.section.restaurant.id
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
