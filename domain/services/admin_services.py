from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from apps.users.models import User
from apps.restaurants.models import Restaurant
from apps.delivery.models import Driver
from apps.orders.models import Order
from domain.events.bus import EventBus
from domain.events.types import (
    UserPromotedToVendor,
    UserPromotedToDriver,
    TempDriverRegistered,
    OrderDisputed,
    OrderResolved,
)
from domain.fsm.order_fsm import OrderFSM, InvalidStateTransitionError
from domain.fsm.payment_fsm import PaymentFSM

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
    def execute(order_id: str, actor, reason: str, category: str = 'other'):
        from apps.orders.models import Dispute
        from apps.payments.models import Payment

        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)

            # Guard: FSM must allow DISPUTED transition
            new_state = OrderFSM.transition(order.status, 'DISPUTED')
            order.status = new_state
            order.save(update_fields=['status'])

            # Guard: one active dispute per category
            existing = Dispute.objects.filter(
                order=order, raised_by=actor, category=category
            ).exclude(
                status__in=['RESOLVED_REFUND', 'RESOLVED_PARTIAL', 'RESOLVED_DISMISSED']
            ).exists()
            if existing:
                raise ValueError(f"An active {category} dispute already exists for this order")

            dispute = Dispute.objects.create(
                order=order,
                raised_by=actor,
                reason=reason,
                category=category,
            )

            # Hold payment if collected
            Payment.objects.filter(
                payable_type='order', payable_id=order.id, status='COLLECTED'
            ).update(status='ON_HOLD')

            EventBus.publish(OrderDisputed(
                aggregate_id=order.id,
                payload={
                    'order_id': str(order.id),
                    'disputed_by': str(actor.id),
                    'reason': reason,
                    'category': category,
                    'dispute_id': str(dispute.id),
                }
            ))

            return dispute


class DisputeResolutionService:
    RESOLUTION_ACTIONS = ('RESOLVED_REFUND', 'RESOLVED_PARTIAL', 'RESOLVED_DISMISSED')

    @staticmethod
    def resolve(dispute_id: str, actor, resolution: str, resolution_note: str = '', refund_amount: Decimal = None):
        from apps.orders.models import Dispute
        from apps.payments.models import Payment
        from domain.services.payment_services import RefundPaymentService

        if resolution not in DisputeResolutionService.RESOLUTION_ACTIONS:
            raise ValueError(f"Invalid resolution: {resolution}")

        with transaction.atomic():
            dispute = Dispute.objects.select_for_update().get(id=dispute_id)
            if dispute.status in ('RESOLVED_REFUND', 'RESOLVED_PARTIAL', 'RESOLVED_DISMISSED'):
                raise ValueError("Dispute is already resolved")

            dispute.status = resolution
            dispute.resolution_note = resolution_note
            dispute.resolved_at = timezone.now()
            dispute.resolved_by = actor
            dispute.save()

            order = Order.objects.select_for_update().get(id=dispute.order_id)

            if resolution == 'RESOLVED_DISMISSED':
                # Release hold — payment goes back to vendor
                Payment.objects.filter(
                    payable_type='order', payable_id=order.id, status='ON_HOLD'
                ).update(status='COLLECTED')
                order.status = OrderFSM.transition(order.status, 'RESOLVED')
                order.save(update_fields=['status'])
                EventBus.publish(OrderResolved(
                    aggregate_id=order.id,
                    payload={
                        'order_id': str(order.id),
                        'resolution': resolution,
                        'dispute_id': str(dispute.id),
                    }
                ))
                return dispute

            # REFUND or PARTIAL: refund the payment
            if refund_amount is None:
                refund_amount = order.total_amount

            RefundPaymentService.execute('order', order.id, override_amount=refund_amount)

            order.status = OrderFSM.transition(order.status, 'RESOLVED')
            order.save(update_fields=['status'])

            EventBus.publish(OrderResolved(
                aggregate_id=order.id,
                payload={
                    'order_id': str(order.id),
                    'resolution': resolution,
                    'refund_amount': str(refund_amount),
                    'dispute_id': str(dispute.id),
                }
            ))

            return dispute

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
