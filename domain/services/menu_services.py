from apps.products.models import Food
from apps.restaurants.models import Restaurant, RestaurantSection, SectionStaff
from django.db import transaction
from domain.events.bus import EventBus
from domain.events.types import FoodCreated, FoodDeleted
from domain.services.permissions import check_restaurant_permission


class MenuService:
    @staticmethod
    def create_food(owner_user, name, price, description='', category='MAIN', is_available=True, image=None, section=None):
        with transaction.atomic():
            restaurant = None
            if section is not None:
                restaurant = section.restaurant
                if not check_restaurant_permission(owner_user, restaurant):
                    raise ValueError("You do not have permission to create foods for this restaurant")
            else:
                restaurant = Restaurant.objects.filter(owner=owner_user).order_by('created_at').first()
                if restaurant is None:
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
                    # Owner-operated restaurants use an implicit default section.
                    first_section = RestaurantSection.objects.filter(restaurant=restaurant).first()
                    if not first_section:
                        first_section = RestaurantSection.objects.create(
                            restaurant=restaurant,
                            name='Main Kitchen',
                            specialization='General',
                            description='Default preparation area',
                            prep_time=restaurant.average_prep_time or 20,
                        )
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
                food = Food.objects.select_related('section__restaurant').get(id=food_id)
            except Food.DoesNotExist:
                raise ValueError("Food not found or you do not have permission to delete it")
            if not check_restaurant_permission(owner_user, food.section.restaurant):
                raise ValueError("You do not have permission to delete this food item")

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
