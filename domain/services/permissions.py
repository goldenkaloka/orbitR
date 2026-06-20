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
    from apps.core.geo_utils import get_neighbors
    from django.db.models import Case, When, Value, IntegerField

    # Get neighbors (k=1 covers immediate surrounding cell hexes)
    neighbor_cells = []
    if restaurant.h3_index:
        neighbor_cells = get_neighbors(restaurant.h3_index, k=1)

    queryset = Driver.objects.filter(
        models.Q(home_restaurant=restaurant) | models.Q(serves_all=True)
    ).exclude(
        user__role__in=('vendor', 'vendor_staff', 'admin')
    ).select_related('user')

    if neighbor_cells:
        queryset = queryset.annotate(
            is_nearby=Case(
                When(h3_index__in=neighbor_cells, then=Value(1)),
                default=Value(0),
                output_field=IntegerField()
            )
        ).order_by('-is_nearby', '-priority')
    else:
        queryset = queryset.order_by('-priority')

    return queryset


def can_driver_delegate(actor, batch):
    """A driver can delegate a batch if they are its currently assigned driver and are a platform driver or supervisor."""
    from apps.delivery.models import Driver
    try:
        driver = Driver.objects.get(user=actor)
    except Driver.DoesNotExist:
        return False
    return batch.driver == driver and (driver.serves_all or driver.can_assign)

import django.db.models as models
