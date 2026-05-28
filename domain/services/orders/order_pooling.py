import logging
import random
import string
from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
from django.db import transaction, IntegrityError
from django.utils import timezone
from apps.orders.models import OrderPool, PoolMember
from apps.restaurants.models import Restaurant
from apps.geo.models import ZoneH3Index
from apps.events.utils import broadcast_pool_status
from domain.services.pricing_service import PricingService
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3
from domain.events.bus import EventBus
from domain.events.types import PoolLocked, PoolPaid, PoolReady, PoolCompleted, PoolCancelled
from domain.fsm.pool_fsm import PoolFSM, InvalidPoolTransitionError
import h3

logger = logging.getLogger(__name__)

LOCKED_POOL_TIMEOUT_MINUTES = 30


def _split_fee_proportionally(total: Decimal, shares: list[Decimal]) -> list[Decimal]:
    """
    Split `total` proportionally by each member's `shares` weight.
    Uses bankers' rounding and distributes any remainder to the largest share.
    Returns a list of Decimal values that sum to exactly `total`.
    """
    total_weight = sum(shares)
    if total_weight <= 0:
        equal_share = (total / max(len(shares), 1)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        return [equal_share] * len(shares)

    results = []
    allocated = Decimal('0')
    for i, weight in enumerate(shares):
        if i == len(shares) - 1:
            results.append(total - allocated)
        else:
            raw = (total * weight / total_weight).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            results.append(raw)
            allocated += raw
    return results


class PoolOrderService:
    MAX_POOL_MEMBERS = 10

    @staticmethod
    def create_pool(initiator, restaurant_id, delivery_address, delivery_location):
        if OrderPool.objects.filter(initiator=initiator, status='OPEN').exists():
            raise ValueError("You already have an open group order.")

        restaurant = Restaurant.objects.get(id=restaurant_id)

        if not delivery_location or not hasattr(delivery_location, 'x'):
            raise ValueError("Valid delivery location (lat/lng) is required for pooling.")

        h3_index = latlng_to_h3(delivery_location.y, delivery_location.x)

        op_zone = ZoneH3Index.objects.filter(
            h3_index=h3_index,
            resolution=ZONE_RESOLUTION,
            zone__is_active=True
        ).select_related('zone').first()

        if not op_zone:
            raise ValueError("Delivery location is not within an operational zone.")

        if restaurant.geo_location:
            restaurant_h3 = latlng_to_h3(restaurant.geo_location.y, restaurant.geo_location.x, ZONE_RESOLUTION)
            if restaurant_h3 and h3_index:
                r_lat, r_lng = h3.cell_to_latlng(restaurant_h3)
                d_lat, d_lng = h3.cell_to_latlng(h3_index)
                distance_km = h3.great_circle_distance((r_lat, r_lng), (d_lat, d_lng))
                if distance_km > restaurant.max_delivery_radius_km:
                    raise ValueError(
                        f"{restaurant.name} does not deliver beyond {restaurant.max_delivery_radius_km}km. "
                        f"Your location is {distance_km:.1f}km away."
                    )

        max_retries = 3
        pool = None
        for i in range(max_retries):
            invite_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            try:
                with transaction.atomic():
                    pool = OrderPool.objects.create(
                        initiator=initiator,
                        restaurant=restaurant,
                        invite_code=invite_code,
                        delivery_address=delivery_address,
                        delivery_location=delivery_location,
                        h3_index=h3_index,
                        expires_at=timezone.now() + timedelta(hours=2)
                    )
                    PoolMember.objects.create(pool=pool, user=initiator, status='ADDING')
                break
            except IntegrityError as e:
                if 'unique_open_pool_per_user' in str(e).lower() or 'initiator' in str(e).lower():
                    raise ValueError("You already have an active group order pool.")
                if i == max_retries - 1:
                    raise ValueError("Failed to generate a unique invite code.")
                continue

        if pool:
            broadcast_pool_status(str(pool.id), 'OPEN')
        return pool

    @staticmethod
    def join_pool(user, invite_code):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(invite_code=invite_code, status='OPEN')

            if pool.expires_at <= timezone.now():
                raise ValueError("This pool has expired.")

            if PoolMember.objects.filter(user=user, pool__status='OPEN').exclude(pool=pool).exists():
                raise ValueError("You are already participating in another active group order.")

            if pool.members.count() >= PoolOrderService.MAX_POOL_MEMBERS:
                raise ValueError(f"Pool is full (max {PoolOrderService.MAX_POOL_MEMBERS}).")

            member, created = PoolMember.objects.get_or_create(pool=pool, user=user, defaults={'status': 'ADDING'})

            if created:
                _pool_id = str(pool.id)
                _username = user.username
                transaction.on_commit(lambda: broadcast_pool_status(_pool_id, 'OPEN', message=f"{_username} joined the pool!"))

            return pool

    @staticmethod
    def add_item(user, pool_id, food_id, quantity):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id, status='OPEN')
            if pool.expires_at <= timezone.now():
                raise ValueError("This pool has expired.")
            member = PoolMember.objects.select_for_update().get(pool=pool, user=user)

            from apps.products.models import Food
            food = Food.objects.get(id=food_id)

            if food.vendor != pool.restaurant:
                raise ValueError("Food must be from the same restaurant as the pool.")

            items = list(member.items)
            found = False
            for item in items:
                if item.get('food_id') == str(food_id):
                    item['quantity'] = item.get('quantity', 0) + quantity
                    item['price'] = float(food.price) * item['quantity']
                    found = True
                    break

            if not found:
                items.append({
                    'food_id': str(food.id),
                    'name': food.name,
                    'quantity': quantity,
                    'price': float(food.price) * quantity,
                    'image_url': str(food.image_url) if hasattr(food, 'image_url') and food.image_url else '',
                })

            member.items = items
            member.items_subtotal = sum(Decimal(str(i['price'])) for i in items)
            member.save(update_fields=['items', 'items_subtotal'])

            _pool_id, _username, _food_name = str(pool.id), user.username, food.name
            transaction.on_commit(lambda: broadcast_pool_status(_pool_id, 'OPEN', message=f"{_username} added {_food_name}"))
            return pool

    @staticmethod
    def remove_item(user, pool_id, food_id, quantity=1):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id, status='OPEN')
            if pool.expires_at <= timezone.now():
                raise ValueError("This pool has expired.")
            member = PoolMember.objects.select_for_update().get(pool=pool, user=user)

            items = list(member.items)
            new_items = []
            for item in items:
                if item.get('food_id') == str(food_id):
                    item_qty = item.get('quantity', 0)
                    remove_qty = min(quantity, item_qty)
                    new_qty = item_qty - remove_qty
                    if new_qty > 0:
                        unit_price = item.get('price', 0) / item_qty if item_qty > 0 else 0
                        item['quantity'] = new_qty
                        item['price'] = float(unit_price * new_qty)
                        new_items.append(item)
                else:
                    new_items.append(item)

            member.items = new_items
            member.items_subtotal = sum(Decimal(str(i['price'])) for i in new_items)
            member.save(update_fields=['items', 'items_subtotal'])

            _pool_id, _username = str(pool.id), user.username
            transaction.on_commit(lambda: broadcast_pool_status(_pool_id, 'OPEN', message=f"{_username} updated items"))
            return pool

    @staticmethod
    def mark_member_ready(user, pool_id):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id, status='OPEN')
            if pool.expires_at <= timezone.now():
                raise ValueError("This pool has expired.")

            member = PoolMember.objects.select_for_update().get(pool=pool, user=user)
            if member.status == 'READY':
                return pool
            if member.items_subtotal <= 0:
                raise ValueError("You must add at least one item.")

            member.status = 'READY'
            member.save(update_fields=['status'])

            _pool_id, _username = str(pool.id), user.username
            transaction.on_commit(lambda: broadcast_pool_status(_pool_id, 'OPEN', message=f"{_username} is ready!"))
            return pool

    @staticmethod
    def leave_pool(user, pool_id):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id, status='OPEN')
            if pool.initiator == user:
                raise PermissionError("Initiator cannot leave — cancel the pool instead.")

            PoolMember.objects.get(pool=pool, user=user).delete()
            _pool_id, _username = str(pool.id), user.username
            transaction.on_commit(lambda: broadcast_pool_status(_pool_id, 'OPEN', message=f"{_username} left."))
            return pool

    @staticmethod
    def lock_pool(pool_id, actor):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)
            if pool.initiator != actor:
                raise PermissionError("Only initiator can lock.")
            if pool.status != 'OPEN':
                raise ValueError("Pool is not OPEN.")

            # Auto-mark the initiator as READY when they lock
            initiator_member = pool.members.filter(user=actor).first()
            if initiator_member and initiator_member.status != 'READY':
                if initiator_member.items_subtotal <= 0:
                    raise ValueError("You must add at least one item before locking.")
                initiator_member.status = 'READY'
                initiator_member.save(update_fields=['status'])

            not_ready = pool.members.exclude(status='READY').values_list('user__username', flat=True)
            if not_ready.exists():
                raise ValueError(f"Members not ready: {', '.join(not_ready)}")

            members = list(pool.members.all())
            if not members:
                raise ValueError("Empty pool.")

            pool.status = PoolFSM.transition(pool.status, 'LOCKED')

            total_fee, s_fee, _ = PricingService.calculate_delivery_fee(pool.restaurant, pool.delivery_location, pool=pool)
            pool.total_delivery_fee = total_fee
            pool.service_fee = s_fee

            subtotals = [m.items_subtotal for m in members]
            delivery_shares = _split_fee_proportionally(total_fee, subtotals)
            service_shares = _split_fee_proportionally(s_fee, subtotals)

            for i, member in enumerate(members):
                member.delivery_fee_share = delivery_shares[i]
                member.service_fee_share = service_shares[i]

            PoolMember.objects.bulk_update(members, ['delivery_fee_share', 'service_fee_share'])

            now = timezone.now()
            pool.locked_at = now
            pool.lock_expires_at = now + timedelta(minutes=LOCKED_POOL_TIMEOUT_MINUTES)
            pool.save(update_fields=['status', 'total_delivery_fee', 'service_fee', 'locked_at', 'lock_expires_at'])

            _pool_id = str(pool.id)
            transaction.on_commit(lambda: broadcast_pool_status(_pool_id, 'LOCKED', message="LOCKED! Time to pay."))
            transaction.on_commit(lambda: EventBus.publish(PoolLocked(
                aggregate_id=pool.id,
                payload={'pool_id': _pool_id, 'member_count': len(members)},
            )))
            return pool

    @staticmethod
    def mark_member_paid(member_id: str, payment_ref: str = ''):
        """
        Called by the payment service after a pool member completes payment.
        Transitions the member to PAID and checks if all members have paid.
        If all paid, transitions the pool to PAID.
        """
        with transaction.atomic():
            member = PoolMember.objects.select_for_update().get(id=member_id)
            pool = OrderPool.objects.select_for_update().get(id=member.pool_id)

            if pool.status != 'LOCKED':
                raise ValueError(f"Pool is not in LOCKED state: {pool.status}")
            if member.status == 'PAID':
                return pool

            member.status = 'PAID'
            member.paid_at = timezone.now()
            member.save(update_fields=['status', 'paid_at'])

            all_paid = not pool.members.exclude(status='PAID').exists()
            if all_paid:
                pool.status = PoolFSM.transition(pool.status, 'PAID')
                pool.paid_at = timezone.now()
                pool.save(update_fields=['status', 'paid_at'])

                _pool_id = str(pool.id)
                transaction.on_commit(lambda: broadcast_pool_status(
                    _pool_id, 'PAID', message="All paid! Restaurant is preparing."
                ))
                transaction.on_commit(lambda: EventBus.publish(PoolPaid(
                    aggregate_id=pool.id,
                    payload={'pool_id': _pool_id, 'all_paid': True},
                )))

            return pool

    @staticmethod
    def mark_pool_ready(pool_id: str, actor):
        """
        Called by the restaurant when all items are ready for dispatch.
        Transitions pool PAID → READY and triggers the batching pipeline.
        """
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)
            if pool.restaurant.owner != actor:
                raise PermissionError("Only the restaurant owner can mark the pool as ready.")

            pool.status = PoolFSM.transition(pool.status, 'READY')
            pool.save(update_fields=['status'])

            _pool_id = str(pool.id)
            transaction.on_commit(lambda: broadcast_pool_status(
                _pool_id, 'READY', message="Ready for dispatch!"
            ))
            transaction.on_commit(lambda: EventBus.publish(PoolReady(
                aggregate_id=pool.id,
                payload={'pool_id': _pool_id},
            )))
            return pool

    @staticmethod
    def cancel_pool(pool_id, actor):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)

            if pool.initiator != actor and actor.role != 'admin':
                raise PermissionError("Only the initiator or an admin can cancel the pool.")

            if not PoolFSM.can_cancel(pool.status):
                raise ValueError(f"Cannot cancel pool in {pool.status} state.")

            pool.status = PoolFSM.transition(pool.status, 'CANCELLED')
            pool.save(update_fields=['status'])

            from domain.services.payment_services import RefundPaymentService
            for member in pool.members.filter(status='PAID'):
                RefundPaymentService.execute('pool_member', member.id)

            _pool_id = str(pool.id)
            _batch_id = str(pool.batch_id) if pool.batch_id else None
            _actor_id = str(actor.id)

            if _batch_id:
                transaction.on_commit(
                    lambda: PoolOrderService._cancel_associated_batch(_batch_id, _pool_id)
                )

            from apps.events.utils import broadcast_pool_status
            transaction.on_commit(lambda: broadcast_pool_status(
                _pool_id, 'CANCELLED', message="Pool has been cancelled by the initiator."
            ))
            transaction.on_commit(lambda: EventBus.publish(PoolCancelled(
                aggregate_id=pool.id,
                payload={
                    'pool_id': _pool_id,
                    'batch_id': _batch_id,
                    'actor_id': _actor_id,
                }
            )))
            return pool

    @staticmethod
    def _cancel_associated_batch(batch_id: str, pool_id: str):
        from apps.orders.models import DeliveryBatch
        from domain.services.driver_free_service import DriverFreeService

        CANCELLABLE_STATES = {'CREATED', 'OFFERED', 'DISPATCHED'}

        try:
            with transaction.atomic():
                batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
                if batch.status not in CANCELLABLE_STATES:
                    logger.warning(
                        f"[PoolOrderService] Batch {batch_id} is in '{batch.status}' "
                        f"— too late to cancel for pool {pool_id}. Manual intervention required."
                    )
                    return

                if batch.status in ('OFFERED', 'DISPATCHED') and batch.driver_id:
                    DriverFreeService.release(batch_id, event='CANCEL', reason=f'Pool {pool_id} was cancelled')
                    batch.refresh_from_db()

                batch.status = 'CANCELLED'
                batch.save(update_fields=['status', 'updated_at'])

                from apps.orders.models import Order
                Order.objects.filter(batch_id=batch_id).update(status='CANCELLED')

                logger.info(f"[PoolOrderService] Cancelled batch {batch_id} (was {batch.status}) for pool {pool_id}")
        except DeliveryBatch.DoesNotExist:
            logger.warning(f"[PoolOrderService] Batch {batch_id} not found during pool cancellation")
        except Exception as exc:
            logger.error(f"[PoolOrderService] Failed to cancel batch {batch_id} for pool {pool_id}: {exc}", exc_info=True)
