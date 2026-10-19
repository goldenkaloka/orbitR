"""
Pay the restaurant at pickup.

The platform settles the restaurant directly when the driver arrives, rather
than after delivery. The driver never carries cash for the food, and the
restaurant is paid before the food leaves the counter.

Accounting: the amount is the restaurant's net entitlement for that order,
``items_subtotal * (1 - commission_rate)`` — the exact figure
``ReleasePaymentService`` would have paid at delivery. Paying it here means
delivery-time release must skip the restaurant (it checks
``order.pickup_paid_amount``), so the restaurant is never paid twice.

Money comes from escrow the customer already paid on checkout
(``platform_wallet.pending_balance``); this is a timing change, not new money.
"""

import logging
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def restaurant_net_share(order):
    """
    What the restaurant is owed for one order — computed exactly the way
    ``ReleasePaymentService`` does, so the pickup and delivery paths can never
    disagree about the same money.
    """
    commission = (
        order.restaurant.commission_rate
        if order.restaurant
        else Decimal('0.050')
    )
    subtotal = Decimal(str(order.items_subtotal or 0))
    return (subtotal * (Decimal('1.0') - Decimal(str(commission)))).quantize(
        Decimal('0.01')
    )


def restaurant_is_platform_owned(restaurant):
    """True only for genuinely company-owned restaurants.

    Admin-created *platform-managed* listings are third-party restaurants and
    still have to be paid; settlement is controlled by ``settlement_mode``.
    """
    return bool(
        restaurant
        and restaurant.settlement_mode == restaurant.SETTLEMENT_COMPANY_OWNED
    )


class PayRestaurantAtPickupService:
    @staticmethod
    def execute(order_id, actor, account_number, network=''):
        """
        Disburse the restaurant's share for this order to their mobile money.

        Idempotent. The PickupDisbursement row is claimed under a unique
        constraint before the gateway is ever called, so a double-tap or a
        concurrent request cannot produce a second real payout.
        """
        from apps.orders.models import Order
        from apps.payments.models import PickupDisbursement
        from domain.events.bus import EventBus
        from domain.events.types import RestaurantPaidAtPickup
        from domain.services.payment_services import (
            debit_escrow_external,
            restore_escrow_external,
        )

        with transaction.atomic():
            # Order.driver is nullable, so select_related('driver') would emit a
            # LEFT OUTER JOIN and Postgres refuses SELECT FOR UPDATE on the
            # nullable side. Compare against driver_id instead of joining.
            order = Order.objects.select_for_update().select_related(
                'restaurant', 'restaurant__owner',
            ).get(id=order_id)

            if actor.role != 'driver':
                raise PermissionError(
                    "Only drivers can pay a restaurant at pickup."
                )

            actor_driver = getattr(actor, 'driver_profile', None)
            if not order.driver_id or actor_driver is None \
                    or order.driver_id != actor_driver.id:
                raise PermissionError(
                    "Only the assigned driver can pay this restaurant."
                )

            if order.status not in ('READY', 'ASSIGNED'):
                raise ValueError(
                    f"Cannot pay restaurant for an order in status {order.status}."
                )

            if restaurant_is_platform_owned(order.restaurant):
                raise ValueError(
                    "This restaurant is company-owned. No mobile-money payout "
                    "is needed — confirm pickup instead."
                )

            if order.restaurant.settlement_mode == order.restaurant.SETTLEMENT_PLATFORM_PREPAID:
                if order.restaurant_payment_status == order.RESTAURANT_PAID:
                    raise ValueError(
                        "OrbitR has already paid this restaurant. Collect only."
                    )
                raise ValueError(
                    "OrbitR pays this restaurant before pickup. Do not pay it "
                    "yourself; wait for the payment confirmation."
                )

            if not order.restaurant or not order.restaurant.geo_location:
                raise ValueError("Order has no restaurant location for pickup.")

            existing = PickupDisbursement.objects.filter(order=order).first()
            if existing:
                if existing.status == PickupDisbursement.STATUS_SUCCESS:
                    return existing
                raise ValueError(
                    "A payment for this order is already in progress or has "
                    "failed. Check its status before retrying."
                )

            amount = restaurant_net_share(order)
            if amount <= 0:
                raise ValueError("Nothing to pay the restaurant for this order.")

            _assert_within_pickup_geofence(actor_driver, order)

            # Claim the row first. This unique constraint is the idempotency
            # guarantee — losing the race means a concurrent request already
            # claimed it, and we must never reach the gateway.
            try:
                with transaction.atomic():
                    disbursement = PickupDisbursement.objects.create(
                        order=order,
                        batch=order.batch,
                        driver=actor_driver,
                        restaurant=order.restaurant,
                        amount=amount,
                        account_number=str(account_number)[:32],
                        network=str(network or '')[:32],
                        status=PickupDisbursement.STATUS_PENDING,
                    )
            except IntegrityError:
                logger.warning(
                    f"[PickupPay] Duplicate payment attempt for order {order.id} "
                    f"— returning existing disbursement."
                )
                return PickupDisbursement.objects.filter(order=order).first()

            # Reserve escrow before sending money. Escrow must cover the payout;
            # failing here beats telling a restaurant to expect cash that the
            # platform does not hold.
            debit_escrow_external(
                amount,
                description=f"Pickup payout for order {str(order.id)[:8]}",
            )

        # Gateway call outside the transaction: network latency must not hold
        # the order lock. The row is already claimed, so this stays safe.
        result = _call_gateway(amount, account_number, order)

        with transaction.atomic():
            disbursement = PickupDisbursement.objects.select_for_update().get(
                id=disbursement.id
            )
            order = Order.objects.select_for_update().get(id=order.id)

            if result.get('success'):
                disbursement.status = PickupDisbursement.STATUS_SUCCESS
                disbursement.gateway_reference = str(
                    result.get('provider_reference') or ''
                )[:255]
                disbursement.failure_reason = ''
                disbursement.settled_at = timezone.now()
                order.pickup_paid_amount = amount
                order.restaurant_payment_status = Order.RESTAURANT_PAID
                order.restaurant_paid_amount = amount
                order.restaurant_payment_reference = disbursement.gateway_reference
                order.restaurant_paid_at = timezone.now()
                order.restaurant_paid_by = actor
                order.save(update_fields=[
                    'pickup_paid_amount', 'restaurant_payment_status',
                    'restaurant_paid_amount', 'restaurant_payment_reference',
                    'restaurant_paid_at', 'restaurant_paid_by', 'updated_at',
                ])
            else:
                disbursement.status = PickupDisbursement.STATUS_FAILED
                disbursement.failure_reason = str(
                    result.get('error') or 'Unknown gateway error'
                )[:2000]
                # Return the money so the driver can retry.
                restore_escrow_external(
                    amount,
                    description=f"Reversal for failed pickup payout {str(order.id)[:8]}",
                )

            disbursement.save()

            if result.get('success'):
                transaction.on_commit(
                    lambda: EventBus.publish(RestaurantPaidAtPickup(
                        aggregate_id=order.id,
                        payload={
                            'amount': str(amount),
                            'restaurant_id': str(order.restaurant_id),
                            'batch_id': str(order.batch_id) if order.batch_id else None,
                            'driver_id': str(order.driver_id),
                        },
                    ))
                )
            else:
                logger.error(
                    f"[PickupPay] Payment failed for order {order.id}: "
                    f"{disbursement.failure_reason}"
                )

        return disbursement


def _assert_within_pickup_geofence(driver, order):
    """Payment must happen at the restaurant, on the same radius as pickup."""
    from apps.core.geo_utils import haversine_distance
    from domain.services.orders.order_fulfillment import pickup_geofence_radius

    if not driver.location or not order.restaurant.geo_location:
        raise ValueError(
            "Location unavailable — cannot verify you are at the restaurant."
        )

    radius = pickup_geofence_radius()
    dist = haversine_distance(driver.location, order.restaurant.geo_location)
    if dist > radius:
        raise ValueError(
            f"You are {int(dist)}m from {order.restaurant.name}. "
            f"Pay the restaurant once you are there ({radius}m limit)."
        )


def _call_gateway(amount, account_number, order):
    from infrastructure.payment_gateways.factory import get_payment_gateway

    gateway = get_payment_gateway()
    reference = f"PICKUP-{str(order.id).replace('-', '')[:20]}"

    try:
        return gateway.disburse(
            amount=amount,
            account_info=account_number,
            reference=reference,
            narration=f"Food payment {str(order.id)[:8]}",
        )
    except Exception as exc:
        logger.error(f"[PickupPay] Gateway raised: {exc}")
        return {'success': False, 'error': str(exc)}