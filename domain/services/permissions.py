def check_restaurant_permission(user, restaurant):
    from apps.restaurants.models import SectionStaff
    if restaurant.owner == user:
        return True
    if SectionStaff.objects.filter(user=user, section__restaurant=restaurant).exists():
        return True
    return False


def can_serve_restaurant(driver, restaurant_id):
    return driver.serves_all or driver.home_restaurant_id == restaurant_id


def restaurant_visible_drivers(restaurant):
    from apps.delivery.models import Driver
    return Driver.objects.filter(
        models.Q(home_restaurant=restaurant) | models.Q(serves_all=True)
    ).select_related('user').order_by('-priority')


def can_driver_delegate(actor, batch):
    """A driver can delegate a batch if they are its currently assigned driver and are a platform driver or supervisor."""
    from apps.delivery.models import Driver
    try:
        driver = Driver.objects.get(user=actor)
    except Driver.DoesNotExist:
        return False
    return batch.driver == driver and (driver.serves_all or driver.can_assign)

import django.db.models as models
