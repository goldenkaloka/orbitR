from django.db import models


def check_restaurant_permission(user, restaurant):
    from apps.restaurants.models import SectionStaff
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
