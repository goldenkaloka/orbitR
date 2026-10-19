from django.db import models


def check_restaurant_permission(user, restaurant):
    from apps.restaurants.models import SectionStaff
    if getattr(user, 'role', None) == 'admin':
        return True
    if restaurant.owner == user:
        return True
    if SectionStaff.objects.filter(user=user, section__restaurant=restaurant).exists():
        return True
    return False


def can_serve_restaurant(driver, restaurant_id):
    return driver.driver_type == 'platform' or driver.home_restaurant_id == restaurant_id


def restaurant_visible_drivers(restaurant):
    from apps.delivery.models import Driver

    return Driver.objects.filter(
        models.Q(home_restaurant=restaurant) | models.Q(driver_type='platform')
    ).exclude(
        user__role__in=('vendor', 'vendor_staff', 'admin')
    ).select_related('user', 'home_zone').order_by('-priority', 'current_active_orders')


def can_driver_delegate(actor, batch):
    """A driver can delegate a batch if they are its currently assigned driver and are a platform driver or supervisor."""
    from apps.delivery.models import Driver
    try:
        driver = Driver.objects.get(user=actor)
    except Driver.DoesNotExist:
        return False
    return batch.driver == driver and (driver.driver_type == 'platform' or driver.can_assign)


def resolve_assignment_source(actor):
    """
    Derive the audit source for a driver allocation from the actor's role.

    Uses serialized_role, not the raw role column: stall staff are stored with
    role='customer' and only derive 'vendor_staff' from having a SectionStaff
    row, so reading the raw column would misclassify them as SYSTEM and stop
    them allocating in restaurant-dispatch mode.
    """
    from apps.delivery.models import DeliveryAssignment

    role = getattr(actor, 'serialized_role', None) or getattr(actor, 'role', None)
    if role == 'admin':
        return DeliveryAssignment.SOURCE_ADMIN
    if role in ('vendor', 'vendor_staff'):
        return DeliveryAssignment.SOURCE_VENDOR
    if role == 'driver' or hasattr(actor, 'driver_profile'):
        return DeliveryAssignment.SOURCE_DRIVER
    return DeliveryAssignment.SOURCE_SYSTEM


def assert_can_allocate_drivers(actor, dispatch_mode):
    """
    Allocation policy, resolved per restaurant (Restaurant.dispatch_mode).

    - admin:      admin allocates. Admins may also allocate as an escalation
                  when the restaurant owns dispatch, so the platform can never
                  be blocked by a quiet restaurant.
    - restaurant: the restaurant allocates its own driver. Admins may still
                  allocate (escalation); other non-admin roles may not.

    Returns the resolved assignment source for audit purposes.
    """
    from apps.delivery.models import DeliveryAssignment
    from apps.restaurants.models import Restaurant

    source = resolve_assignment_source(actor)

    if source == DeliveryAssignment.SOURCE_ADMIN:
        return source

    if dispatch_mode == Restaurant.DISPATCH_RESTRANT:
        if source == DeliveryAssignment.SOURCE_VENDOR:
            return source
        raise PermissionError(
            "This restaurant handles its own driver allocation."
        )

    raise PermissionError(
        "Driver allocation for this restaurant is handled by admin dispatch."
    )
