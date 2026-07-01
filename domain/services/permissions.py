from django.conf import settings
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.db import models


DEFAULT_DRIVER_RESTAURANT_RADIUS_KM = 5
DEFAULT_DRIVER_RESTAURANT_ZONE_RADIUS = 0


def check_restaurant_permission(user, restaurant):
    from apps.restaurants.models import SectionStaff
    if restaurant.owner == user:
        return True
    if SectionStaff.objects.filter(user=user, section__restaurant=restaurant).exists():
        return True
    return False


def can_serve_restaurant(driver, restaurant_id):
    return driver.serves_all or driver.home_restaurant_id == restaurant_id


def _dispatch_radius_km(driver=None):
    configured_radius = getattr(
        settings,
        'DRIVER_RESTAURANT_MAX_DISTANCE_KM',
        DEFAULT_DRIVER_RESTAURANT_RADIUS_KM,
    )
    try:
        platform_radius = float(configured_radius)
    except (TypeError, ValueError):
        platform_radius = DEFAULT_DRIVER_RESTAURANT_RADIUS_KM

    if driver is None:
        return platform_radius

    try:
        driver_radius = float(driver.max_radius_km)
    except (TypeError, ValueError):
        driver_radius = platform_radius
    return max(0.1, min(platform_radius, driver_radius))


def _dispatch_zone_radius():
    configured_radius = getattr(
        settings,
        'DRIVER_RESTAURANT_ZONE_RADIUS',
        DEFAULT_DRIVER_RESTAURANT_ZONE_RADIUS,
    )
    try:
        zone_radius = int(configured_radius)
    except (TypeError, ValueError):
        zone_radius = DEFAULT_DRIVER_RESTAURANT_ZONE_RADIUS
    return max(0, min(zone_radius, 2))


def _restaurant_zone_ids(restaurant):
    if not restaurant.h3_index:
        return []

    from apps.core.geo_utils import get_zone_ids_from_h3
    return get_zone_ids_from_h3(restaurant.h3_index, radius=_dispatch_zone_radius())


def _is_live_driver(driver):
    return driver.status in ('AVAILABLE', 'ONLINE')


def _home_zone_matches(driver, restaurant_zone_ids):
    return bool(driver.home_zone_id and driver.home_zone_id in restaurant_zone_ids)


def _offline_driver_matches_base(driver, restaurant, restaurant_zone_ids):
    if _home_zone_matches(driver, restaurant_zone_ids):
        return True
    return bool(driver.home_restaurant_id and driver.home_restaurant_id == restaurant.id)


def can_reach_restaurant(driver, restaurant):
    if not can_serve_restaurant(driver, restaurant.id):
        return False

    restaurant_zone_ids = _restaurant_zone_ids(restaurant)
    if not _is_live_driver(driver):
        return _offline_driver_matches_base(driver, restaurant, restaurant_zone_ids)

    if restaurant_zone_ids:
        if not driver.h3_index:
            return False
        from apps.core.geo_utils import get_zone_ids_from_h3
        driver_zone_ids = get_zone_ids_from_h3(driver.h3_index, radius=0)
        if not set(restaurant_zone_ids).intersection(driver_zone_ids):
            return False

    if restaurant.geo_location and not driver.location:
        return False
    if not restaurant.geo_location or not driver.location:
        return bool(restaurant_zone_ids)

    from apps.core.geo_utils import haversine_distance
    distance_km = haversine_distance(restaurant.geo_location, driver.location) / 1000
    return distance_km <= _dispatch_radius_km(driver)


def restaurant_visible_drivers(restaurant):
    from apps.delivery.models import Driver

    queryset = Driver.objects.filter(
        models.Q(home_restaurant=restaurant) | models.Q(serves_all=True)
    ).exclude(
        user__role__in=('vendor', 'vendor_staff', 'admin')
    ).select_related('user', 'home_zone')

    restaurant_zone_ids = _restaurant_zone_ids(restaurant)
    if restaurant_zone_ids:
        from apps.geo.models import ZoneH3Index
        zone_cells = ZoneH3Index.objects.filter(
            zone_id__in=restaurant_zone_ids,
            zone__is_active=True,
        ).values('h3_index')
        live_driver_filter = models.Q(
            status__in=('AVAILABLE', 'ONLINE'),
            h3_index__in=zone_cells,
        )
        offline_home_filter = ~models.Q(
            status__in=('AVAILABLE', 'ONLINE')
        ) & (
            models.Q(home_zone_id__in=restaurant_zone_ids) |
            models.Q(home_restaurant=restaurant)
        )
        queryset = queryset.filter(live_driver_filter | offline_home_filter)

    if restaurant.geo_location:
        live_distance_filter = models.Q(
            status__in=('AVAILABLE', 'ONLINE'),
            location__isnull=False,
            location__dwithin=(restaurant.geo_location, D(km=_dispatch_radius_km())),
        )
        offline_home_filter = ~models.Q(
            status__in=('AVAILABLE', 'ONLINE')
        ) & (
            models.Q(home_zone_id__in=restaurant_zone_ids) |
            models.Q(home_restaurant=restaurant)
        )
        queryset = queryset.filter(
            live_distance_filter | offline_home_filter
        ).annotate(
            distance_meters=Distance('location', restaurant.geo_location),
            availability_rank=models.Case(
                models.When(status__in=('AVAILABLE', 'ONLINE'), then=models.Value(0)),
                default=models.Value(1),
                output_field=models.IntegerField(),
            ),
        ).order_by('availability_rank', 'distance_meters', '-priority', 'current_active_orders')
        return [driver for driver in queryset if can_reach_restaurant(driver, restaurant)]
    else:
        queryset = queryset.order_by('-priority', 'current_active_orders')

    return queryset


def can_driver_delegate(actor, batch):
    """A driver can delegate a batch if they are its currently assigned driver and are a platform driver or supervisor."""
    from apps.delivery.models import Driver
    try:
        driver = Driver.objects.get(user=actor)
    except Driver.DoesNotExist:
        return False
    return batch.driver == driver and (driver.serves_all or driver.can_assign)
