import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task
def learn_address_place_alias(address_id):
    from apps.users.models import UserAddress

    from .place_aliases import record_address_place_alias

    try:
        address = UserAddress.objects.select_related('zone').get(id=address_id)
    except UserAddress.DoesNotExist:
        return None
    alias = record_address_place_alias(address)
    return str(alias.id) if alias else None


@shared_task
def learn_order_place_alias(order_id):
    from apps.orders.models import Order

    from .place_aliases import record_order_place_alias

    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return None
    alias = record_order_place_alias(order)
    return str(alias.id) if alias else None
