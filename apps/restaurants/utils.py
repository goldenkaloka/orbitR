from apps.orders.models import Order
from apps.restaurants.models import RestaurantSection
from django.db.models import Count

BUSY_SECTION_THRESHOLD = 5


def compute_busy_restaurant_ids(restaurant_ids):
    """Return set of str restaurant IDs that have any section at or over capacity."""
    if not restaurant_ids:
        return set()

    active_counts = list(
        Order.objects.filter(
            restaurant_id__in=restaurant_ids,
            batch__status='ACCUMULATING',
            # Order statuses are uppercase (OrderFSM / order_status_valid
            # constraint) — the old lowercase literals never matched, so the
            # busy badge was permanently off.
            status__in=['CONFIRMED', 'PREPARING'],
        ).values('restaurant_id', 'items__section').annotate(cnt=Count('id'))
    )

    thresholds = dict(
        RestaurantSection.objects.filter(restaurant_id__in=restaurant_ids)
        .values_list('id', 'max_concurrent_orders')
    )

    busy_ids = set()
    for entry in active_counts:
        section_id = entry['items__section']
        if section_id and entry['cnt'] >= thresholds.get(section_id, BUSY_SECTION_THRESHOLD):
            busy_ids.add(str(entry['restaurant_id']))

    return busy_ids
