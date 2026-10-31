from decimal import Decimal

import logging

from apps.delivery.models import Driver
from apps.orders.models import DeliveryBatch, Order
from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.db import transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import (
    OrderDisputed,
    OrderResolved,
    TempDriverRegistered,
    UserPromotedToDriver,
    UserPromotedToVendor,
)
from domain.fsm.order_fsm import InvalidStateTransitionError, OrderFSM
from domain.fsm.payment_fsm import PaymentFSM

logger = logging.getLogger(__name__)


def _renotify_batches(batch_ids):
    """Re-run dispatch alerting for batches whose owner of allocation changed."""
    from domain.services.dispatch_tasks import dispatch_batch_task

    for batch_id in batch_ids:
        try:
            dispatch_batch_task.delay(batch_id)
        except Exception as exc:
            # Never fail the admin's edit because a Celery broker hiccuped.
            logger.error(f"Failed to re-notify batch {batch_id}: {exc}")


class PromoteUserService:
    @staticmethod
    def to_vendor(user_id: str, restaurant_name: str, location: str, description: str = '', category: str = 'General', latitude=None, longitude=None, image=None, logo=None, dispatch_mode=None):
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

            valid_dispatch_modes = {choice[0] for choice in Restaurant.DISPATCH_MODE_CHOICES}
            if dispatch_mode is not None:
                if dispatch_mode not in valid_dispatch_modes:
                    raise ValueError(
                        "Invalid dispatch_mode. Expected one of: "
                        f"{', '.join(sorted(valid_dispatch_modes))}"
                    )
            else:
                dispatch_mode = Restaurant.DISPATCH_ADMIN

            restaurant = Restaurant.objects.create(
                owner=user,
                name=restaurant_name,
                location=location,
                description=description,
                category=category,
                geo_location=geo_location,
                image=image,
                logo=logo,
                dispatch_mode=dispatch_mode,
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
    def to_driver(user_id: str, **extra_fields):
        """
        Admin promotes a user to platform driver.
        Platform drivers (driver_type='platform') can optionally be assigned a
        home_zone — they can then serve all restaurants within that zone.
        """
        with transaction.atomic():
            user = User.objects.select_for_update().get(id=user_id)
            if user.role != 'customer':
                raise ValueError(f"Cannot promote user with role={user.role}. Only customers can be promoted.")
            _ensure_user_can_be_driver(user)

            user.role = 'driver'
            user.save(update_fields=['role'])

            defaults = {'status': 'OFFLINE', 'driver_type': 'platform'}
            allowed_fields = {
                'can_assign', 'priority', 'home_restaurant_id',
                'home_zone_id', 'max_active_orders', 'vehicle_type',
            }
            for key in allowed_fields:
                if key in extra_fields and extra_fields[key] is not None:
                    defaults[key] = extra_fields[key]
            defaults['driver_type'] = 'platform'

            driver, created = Driver.objects.get_or_create(
                user=user,
                defaults=defaults
            )

            if not created:
                for key, value in defaults.items():
                    setattr(driver, key, value)
                driver.save()

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

        valid_categories = {choice[0] for choice in Dispute.CATEGORY_CHOICES}
        if category not in valid_categories:
            raise ValueError("Invalid dispute category")
        if not reason or not str(reason).strip():
            raise ValueError("Dispute reason is required")

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

            # Hold payment if collected — use FSM to validate transition
            for payment in Payment.objects.filter(
                payable_type='order', payable_id=order.id, status='COLLECTED'
            ):
                payment.status = PaymentFSM.transition(payment.status, 'ON_HOLD')
                payment.save(update_fields=['status'])

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
    def resolve(dispute_id: str, actor, resolution: str, resolution_note: str = '', refund_amount: Decimal = None,
                refund_to_wallet: bool = False):
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
                # Release hold — payment goes back to vendor via FSM
                for payment in Payment.objects.filter(
                    payable_type='order', payable_id=order.id, status='ON_HOLD'
                ):
                    payment.status = PaymentFSM.transition(payment.status, 'COLLECTED')
                    payment.save(update_fields=['status'])
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
            if refund_amount is not None and refund_amount <= 0:
                raise ValueError("Refund amount must be greater than zero")
            if resolution == 'RESOLVED_PARTIAL' and refund_amount is None:
                raise ValueError("Partial dispute resolution requires refund_amount")
            if refund_amount is not None and refund_amount > order.total_amount:
                raise ValueError("Refund amount cannot exceed the order total")
            if resolution == 'RESOLVED_PARTIAL' and refund_amount >= order.total_amount:
                raise ValueError("Partial refund must be less than the order total")
            if refund_amount is None:
                refund_amount = order.total_amount

            RefundPaymentService.execute(
                'order', order.id,
                override_amount=refund_amount,
                refund_to_wallet=refund_to_wallet,
            )

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
        """
        Vendor registers a customer as a restaurant driver.
        Restaurant drivers (driver_type='restaurant') are tied to exactly one
        restaurant via home_restaurant. They can only serve their own restaurant.
        """
        with transaction.atomic():
            user = User.objects.select_for_update().get(id=user_id)
            restaurant = Restaurant.objects.get(id=restaurant_id)

            if user.role in ('vendor', 'vendor_staff', 'admin'):
                raise ValueError("Restaurant owners, staff, and admins cannot be registered as drivers.")
            _ensure_user_can_be_driver(user)

            driver, created = Driver.objects.get_or_create(
                user=user,
                defaults={
                    'home_restaurant': restaurant,
                    'driver_type': 'restaurant',
                    'status': 'ONLINE',
                }
            )

            if not created:
                return driver

            if user.role == 'customer':
                user.role = 'driver'
                user.save(update_fields=['role'])

            EventBus.publish(TempDriverRegistered(
                aggregate_id=driver.id,
                payload={
                    'driver_id': str(driver.id),
                    'user_id': str(user.id),
                    'home_restaurant': str(restaurant.id)
                }
            ))

            return driver


def _ensure_user_can_be_driver(user):
    from apps.restaurants.models import SectionStaff
    if Restaurant.objects.filter(owner=user).exists():
        raise ValueError("Restaurant owners cannot be promoted or registered as drivers.")
    if SectionStaff.objects.filter(user=user).exists():
        raise ValueError("Restaurant stall staff cannot be promoted or registered as drivers while assigned to a stall.")

class ManageRestaurantService:
    ALLOWED_UPDATE_FIELDS = {
        'name',
        'location',
        'description',
        'category',
        'image',
        'logo',
        'source_type',
        'public_disclaimer',
        'dispatch_mode',
        'settlement_mode',
        'payout_phone',
        'payout_network',
        'payout_account_name',
    }

    @staticmethod
    def create_restaurant(owner, name, location='', description='', category='General',
                          latitude=None, longitude=None, image=None, logo=None,
                          dispatch_mode=None, source_type=None, public_disclaimer='',
                          settlement_mode=None, payout_phone='', payout_network='',
                          payout_account_name=''):
        """Create a restaurant owned by `owner` (normally the acting admin).

        Unlike PromoteUserService.to_vendor this needs no customer to convert:
        an admin can stand up an ops-owned restaurant directly and then run it
        from the admin "Restaurants" tab.
        """
        from django.contrib.gis.geos import Point

        if not name or not str(name).strip():
            raise ValueError("Restaurant name is required")

        valid_dispatch_modes = {choice[0] for choice in Restaurant.DISPATCH_MODE_CHOICES}
        if dispatch_mode is not None:
            if dispatch_mode not in valid_dispatch_modes:
                raise ValueError(
                    "Invalid dispatch_mode. Expected one of: "
                    f"{', '.join(sorted(valid_dispatch_modes))}"
                )
        else:
            dispatch_mode = Restaurant.DISPATCH_ADMIN

        if source_type is not None:
            valid_source_types = {choice[0] for choice in Restaurant.SOURCE_TYPE_CHOICES}
            if source_type not in valid_source_types:
                raise ValueError("Invalid restaurant source_type")
        else:
            source_type = Restaurant.SOURCE_PARTNER

        geo_location = None
        if latitude is not None and longitude is not None:
            try:
                geo_location = Point(float(longitude), float(latitude))
            except (ValueError, TypeError):
                pass

        if settlement_mode is None:
            settlement_mode = (
                Restaurant.SETTLEMENT_PLATFORM_PREPAID
                if source_type == Restaurant.SOURCE_PLATFORM_MANAGED
                else Restaurant.SETTLEMENT_VENDOR_PAYOUT
            )

        with transaction.atomic():
            return Restaurant.objects.create(
                owner=owner,
                name=name,
                location=location or '',
                description=description or '',
                category=category or 'General',
                geo_location=geo_location,
                image=image,
                logo=logo,
                dispatch_mode=dispatch_mode,
                source_type=source_type,
                public_disclaimer=public_disclaimer or '',
                settlement_mode=settlement_mode,
                payout_phone=payout_phone or '',
                payout_network=payout_network or '',
                payout_account_name=payout_account_name or '',
            )

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
            owner_id = kwargs.pop('owner_id', None)

            if owner_id is not None:
                # Ownership reassignment: an admin can take ownership of a
                # restaurant (or hand it to another admin/vendor) so the admin
                # app can operate it as an "ops-owned" property. owner_id is
                # NOT NULL in the schema, so an empty value is treated as "no
                # change" rather than clearing the owner.
                if owner_id not in ('', 'null', 'None'):
                    owner = User.objects.filter(id=owner_id).first()
                    if owner is None:
                        raise ValueError("Owner user not found")
                    if owner.role not in ('admin', 'vendor'):
                        raise ValueError("Owner must have role admin or vendor")
                    restaurant.owner = owner

            if lat is not None and lng is not None:
                try:
                    restaurant.geo_location = Point(float(lng), float(lat))
                except (ValueError, TypeError):
                    pass
            elif lat is not None and restaurant.geo_location:
                 restaurant.geo_location = Point(restaurant.geo_location.x, float(lat))
            elif lng is not None and restaurant.geo_location:
                 restaurant.geo_location = Point(float(lng), restaurant.geo_location.y)
                 

            previous_dispatch_mode = restaurant.dispatch_mode

            for key, value in kwargs.items():
                if key in ManageRestaurantService.ALLOWED_UPDATE_FIELDS and value is not None:
                    if key == 'source_type':
                        valid_source_types = {choice[0] for choice in Restaurant.SOURCE_TYPE_CHOICES}
                        if value not in valid_source_types:
                            raise ValueError("Invalid restaurant source_type")
                    if key == 'dispatch_mode':
                        valid_dispatch_modes = {choice[0] for choice in Restaurant.DISPATCH_MODE_CHOICES}
                        if value not in valid_dispatch_modes:
                            raise ValueError(
                                "Invalid dispatch_mode. Expected one of: "
                                f"{', '.join(sorted(valid_dispatch_modes))}"
                            )
                    if key == 'settlement_mode':
                        valid_modes = {choice[0] for choice in Restaurant.SETTLEMENT_CHOICES}
                        if value not in valid_modes:
                            raise ValueError(
                                "Invalid settlement_mode. Expected one of: "
                                f"{', '.join(sorted(valid_modes))}"
                            )
                    setattr(restaurant, key, value)
            restaurant.save()

            # Handing dispatch authority to the restaurant must also hand over
            # the batches already waiting — otherwise orders already alerted to
            # admins sit unassigned with nobody re-notified, and batched orders
            # are exempt from the unassigned-expiry task. Clearing offer_sent_at
            # resets the alert throttle so the new audience is told immediately.
            if restaurant.dispatch_mode != previous_dispatch_mode:
                affected = list(
                    DeliveryBatch.objects
                    .filter(
                        status__in=['PENDING_DRIVER', 'PRE_ASSIGNING'],
                        orders__restaurant_id=restaurant.id,
                    )
                    .values_list('id', flat=True)
                    .distinct()
                )
                if affected:
                    DeliveryBatch.objects.filter(id__in=affected).update(
                        offer_sent_at=None
                    )
                    transaction.on_commit(
                        lambda ids=[str(b) for b in affected]: _renotify_batches(ids)
                    )

            return restaurant


    @staticmethod
    def delete_restaurant(restaurant_id: str):
        with transaction.atomic():
            restaurant = Restaurant.objects.select_for_update().get(id=restaurant_id)
            restaurant.delete()
            return True

class UpdateDriverService:
    @staticmethod
    def execute(driver_id: str, **fields):
        with transaction.atomic():
            driver = Driver.objects.select_for_update().get(id=driver_id)
            allowed = {
                'status', 'driver_type', 'can_assign', 'priority',
                'home_zone_id', 'max_active_orders', 'current_active_orders',
                'max_radius_km',
            }
            for key, value in fields.items():
                if key in allowed and value is not None:
                    setattr(driver, key, value)
            driver.save()
            driver.refresh_from_db()
            return driver
