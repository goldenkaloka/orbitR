from django.db import transaction
from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.delivery.models import Driver
from apps.orders.models import Order
from domain.events.bus import EventBus
from domain.events.types import (
    UserPromotedToVendor,
    UserPromotedToDriver,
    TempDriverRegistered,
    OrderDisputed
)
from domain.fsm.order_fsm import OrderFSM, InvalidStateTransitionError

class PromoteUserService:
    @staticmethod
    def to_vendor(user_id: str, restaurant_name: str, location: str, description: str = '', category: str = 'General', latitude=None, longitude=None, image=None):
        with transaction.atomic():
            user = User.objects.select_for_update().get(id=user_id)
            if user.role != 'customer':
                raise ValueError(f"Cannot promote user with role={user.role}. Only customers can be promoted.")

            user.role = 'vendor'
            user.save(update_fields=['role'])

            from django.contrib.gis.geos import Point
            
            geo_location = None
            if latitude is not None and longitude is not None:
                try:
                    geo_location = Point(float(longitude), float(latitude))
                except (ValueError, TypeError):
                    pass

            restaurant = Restaurant.objects.create(
                owner=user,
                name=restaurant_name,
                location=location,
                description=description,
                category=category,
                geo_location=geo_location,
                image=image
            )


            EventBus.publish(UserPromotedToVendor(
                aggregate_id=user.id,
                payload={
                    'user_id': str(user.id),
                    'restaurant_id': str(restaurant.id)
                }
            ))

            return user, restaurant

    @staticmethod
    def to_driver(user_id: str):
        with transaction.atomic():
            user = User.objects.select_for_update().get(id=user_id)
            if user.role != 'customer':
                raise ValueError(f"Cannot promote user with role={user.role}. Only customers can be promoted.")

            user.role = 'driver'
            user.save(update_fields=['role'])

            driver, created = Driver.objects.get_or_create(
                user=user,
                defaults={'driver_type': 'permanent', 'status': 'OFFLINE'}
            )
            if not created:
                driver.driver_type = 'permanent'
                driver.save(update_fields=['driver_type'])

            EventBus.publish(UserPromotedToDriver(
                aggregate_id=user.id,
                payload={
                    'user_id': str(user.id),
                    'driver_id': str(driver.id)
                }
            ))

            return user, driver

class DisputeOrderService:
    @staticmethod
    def execute(order_id: str, actor, reason: str):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            
            from apps.orders.models import Dispute
            dispute = Dispute.objects.create(
                order=order,
                raised_by=actor,
                reason=reason
            )
            
            # Validates transition via FSM
            try:
                new_state = OrderFSM.transition(order.status, 'DISPUTED')
                order.status = new_state
                order.save(update_fields=['status'])
            except InvalidStateTransitionError:
                pass # Keep state transition errors silent if FSM fails but record created

            EventBus.publish(OrderDisputed(
                aggregate_id=order.id,
                payload={'order_id': str(order.id), 'disputed_by': str(actor.id), 'reason': reason}
            ))

            return order

class RegisterTempDriverService:
    @staticmethod
    def execute(user_id: str, restaurant_id: str):
        with transaction.atomic():
            user = User.objects.select_for_update().get(id=user_id)
            restaurant = Restaurant.objects.get(id=restaurant_id)

            driver, created = Driver.objects.get_or_create(
                user=user,
                defaults={
                    'driver_type': 'temporary',
                    'assigned_by': restaurant,
                    'status': 'ONLINE',
                }
            )
            
            if not created and driver.driver_type == 'permanent':
                raise ValueError('This user is already a permanent driver')

            if user.role == 'customer':
                user.role = 'driver'
                user.save(update_fields=['role'])

            EventBus.publish(TempDriverRegistered(
                aggregate_id=driver.id,
                payload={
                    'driver_id': str(driver.id),
                    'user_id': str(user.id),
                    'assigned_by': str(restaurant.id)
                }
            ))

            return driver

class ManageRestaurantService:
    @staticmethod
    def toggle_status(restaurant_id: str):
        with transaction.atomic():
            restaurant = Restaurant.objects.select_for_update().get(id=restaurant_id)
            restaurant.is_active = not restaurant.is_active
            restaurant.save(update_fields=['is_active'])
            return restaurant

    @staticmethod
    def update_restaurant(restaurant_id: str, **kwargs):
        with transaction.atomic():
            from django.contrib.gis.geos import Point
            restaurant = Restaurant.objects.select_for_update().get(id=restaurant_id)
            
            lat = kwargs.pop('latitude', None)
            lng = kwargs.pop('longitude', None)
            
            if lat is not None and lng is not None:
                try:
                    restaurant.geo_location = Point(float(lng), float(lat))
                except (ValueError, TypeError):
                    pass
            elif lat is not None and restaurant.geo_location:
                 restaurant.geo_location = Point(restaurant.geo_location.x, float(lat))
            elif lng is not None and restaurant.geo_location:
                 restaurant.geo_location = Point(float(lng), restaurant.geo_location.y)
                 

            for key, value in kwargs.items():
                if value is not None:
                    setattr(restaurant, key, value)
            restaurant.save()
            return restaurant


    @staticmethod
    def delete_restaurant(restaurant_id: str):
        with transaction.atomic():
            restaurant = Restaurant.objects.select_for_update().get(id=restaurant_id)
            restaurant.delete()
            return True
