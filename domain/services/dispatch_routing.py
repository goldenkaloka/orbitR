"""
Dispatch routing policy.

Answers one question: for a given order or batch, who is expected to allocate
the driver?

The authority is set per restaurant (Restaurant.dispatch_mode), decided at
creation and editable by an admin. A batch that spans restaurants resolves by
the most restrictive rule: if ANY participating restaurant is admin-dispatch,
the admin owns it. This keeps platform visibility rather than silently handing
a mixed batch to a restaurant that only owns part of it.

Admins may always allocate, in either mode. That is deliberate: it is the
escalation path when a restaurant goes quiet, and it means a stale client can
never block the platform from dispatching.
"""

import logging

logger = logging.getLogger(__name__)


def resolve_order_dispatch_mode(order):
    """Dispatch authority for a single order, from its restaurant."""
    restaurant = getattr(order, 'restaurant', None)
    if restaurant is None:
        # No restaurant means no restaurant to hand it back to — the platform
        # is the only possible allocator.
        return 'admin'
    return restaurant.dispatch_mode or 'admin'


def resolve_batch_dispatch_mode(batch):
    """
    Dispatch authority for a batch across all of its restaurants.

    Admin wins if any participating restaurant is admin-dispatch.
    """
    from apps.restaurants.models import Restaurant

    restaurant_ids = batch.orders.values_list('restaurant_id', flat=True)
    modes = set(
        Restaurant.objects
        .filter(id__in=list(restaurant_ids))
        .values_list('dispatch_mode', flat=True)
    )

    if not modes:
        logger.warning(
            f"[dispatch_routing] Batch {batch.id} has no restaurants — defaulting to admin dispatch."
        )
        return Restaurant.DISPATCH_ADMIN

    if Restaurant.DISPATCH_ADMIN in modes:
        return Restaurant.DISPATCH_ADMIN

    return Restaurant.DISPATCH_RESTRANT