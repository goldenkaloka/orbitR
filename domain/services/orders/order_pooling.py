import logging
import random
import string
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

import h3
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3
from apps.delivery.tasks import notify_nearby_pool_created
from apps.events.utils import broadcast_pool_status
from apps.geo.models import ZoneH3Index
from apps.orders.models import Order, OrderItem, OrderPool, PoolMember
from apps.products.models import Food
from apps.restaurants.models import MealPeriod, Restaurant
from django.db import IntegrityError, transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import (
    OrderCancelled,
    OrderCreated,
    PoolCancelled,
    PoolCompleted,
    PoolLocked,
    PoolPaid,
    PoolReady,
)
from domain.fsm.pool_fsm import InvalidPoolTransitionError, PoolFSM
from domain.services.orders.scheduled_orders import (
    ensure_preorder_slot_capacity,
    meal_window_for,
    release_at_for,
    validate_scheduled_for,
)
from domain.services.pricing_service import PricingService

logger = logging.getLogger(__name__)

LOCKED_POOL_TIMEOUT_MINUTES = 30

# Orders a cancelled bucket may still own. A batch that already left the
# kitchen is out of this set on purpose, and a picked order must never be
# flipped to CANCELLED without going through the driver-release flow.
CANCELLABLE_POOL_ORDER_STATUSES = (
    'PENDING_PAYMENT',
    'SCHEDULED',
    'CREATED',
    'AWAITING_CONFIRMATION',
    'CONFIRMED',
    'PREPARING',
    'READY',
    'ASSIGNED',
)


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


def _drop_fee_shares(pool, members):
    """Flat per-order fees for a bucket pointed at a scheduled drop.

    A drop fee is charged per order, not per trip: eight people at a gate each
    pay 500. The normal bucket splits one trip's fee proportionally, which
    would let a large order subsidise everyone else's delivery.
    """
    if not getattr(pool, 'drop_window_id', None):
        return None
    per_order_fee = Decimal(str(pool.drop_window.delivery_fee))
    policy = PricingService.get_active_policy()
    per_order_service = (
        Decimal(str(policy.service_fee)) if policy else Decimal('300')
    )
    count = max(len(members), 1)
    return (
        per_order_fee * count,
        per_order_service * count,
        [per_order_fee] * len(members),
        [per_order_service] * len(members),
    )


def _notify_pool_prep_sms(pool_id: str):
    try:
        from domain.services.order_sms_notifications import OrderPrepSmsService
        OrderPrepSmsService.notify_pool_orders(pool_id)
    except Exception as exc:
        logger.error(f"[PoolPrepSMS] Failed to notify restaurant for pool {pool_id}: {exc}")


class PoolOrderService:
    MAX_POOL_MEMBERS = 10

    @staticmethod
    def _refresh_fee_preview(pool):
        """Price an OPEN bucket so the card never reads 0 before it is locked.

        The fixed delivery fee is re-split every time the squad changes; the
        lock step recomputes the same total and logs it.
        """
        members = list(pool.members.all())
        drop_fees = _drop_fee_shares(pool, members)
        if drop_fees:
            total_fee, service_fee, delivery_shares, service_shares = drop_fees
        else:
            total_fee, service_fee, _ = PricingService.calculate_delivery_fee(
                pool.restaurant,
                pool.delivery_location,
                pool=pool,
                skip_log=True,
            )
        pool.total_delivery_fee = total_fee
        pool.service_fee = service_fee
        pool.save(update_fields=['total_delivery_fee', 'service_fee'])

        if not members:
            return

        if not drop_fees:
            subtotals = [member.items_subtotal for member in members]
            delivery_shares = _split_fee_proportionally(total_fee, subtotals)
            service_shares = _split_fee_proportionally(service_fee, subtotals)
        for i, member in enumerate(members):
            member.delivery_fee_share = delivery_shares[i]
            member.service_fee_share = service_shares[i]
        PoolMember.objects.bulk_update(
            members, ['delivery_fee_share', 'service_fee_share'],
        )

    @staticmethod
    def create_pool(
        initiator,
        restaurant_id,
        delivery_address,
        delivery_location,
        scheduled_for=None,
        meal_period=None,
        drop_window=None,
    ):
        if OrderPool.objects.filter(initiator=initiator, status='OPEN').exists():
            raise ValueError("You already have an open bucket.")

        restaurant = Restaurant.objects.get(id=restaurant_id)

        # A bucket pointed at a scheduled drop becomes a group order for a
        # hostel/campus window: the point supplies the address, the window
        # supplies the flat fee, and the cut-off (not the kitchen release time)
        # closes the bucket.
        drop_start = None
        if drop_window is not None:
            from apps.orders.models import DropWindow
            from domain.services.orders.drop_service import (
                DropUnavailable,
                validate_drop_booking,
            )

            if not isinstance(drop_window, DropWindow):
                try:
                    drop_window = DropWindow.objects.select_related('drop_point').get(
                        id=drop_window,
                    )
                except (DropWindow.DoesNotExist, ValueError, TypeError):
                    raise ValueError("This drop window does not exist.")
            try:
                drop_start = validate_drop_booking(
                    drop_window, scheduled_for, restaurant=restaurant,
                )
            except DropUnavailable as exc:
                raise ValueError(str(exc))
            scheduled_for = drop_start
            delivery_location = drop_window.drop_point.geo_location
            delivery_address = delivery_address or drop_window.drop_point.name

        if not delivery_location or not hasattr(delivery_location, 'x'):
            raise ValueError("Valid delivery location (lat/lng) is required for pooling.")

        requested_meal = (meal_period or '').strip().upper() or None
        if requested_meal and not scheduled_for:
            raise ValueError("Choose an eating time for a pre-order bucket.")
        if requested_meal and requested_meal not in dict(MealPeriod.KIND_CHOICES):
            raise ValueError(f"Unknown meal period: {requested_meal}")

        if scheduled_for and not drop_start:
            validate_scheduled_for(scheduled_for, restaurant=restaurant)
        if scheduled_for:
            # The booking must fall inside a serving window, and the bucket's
            # meal must be the one that window serves. A time outside every
            # window is rejected later by ensure_preorder_slot_capacity with
            # the user-facing "only accepted during..." message.
            window = meal_window_for(restaurant, scheduled_for)
            if window is not None:
                if requested_meal and requested_meal != window.kind:
                    raise ValueError(
                        f"This time belongs to the {window.label.lower()} menu, "
                        f"not {requested_meal.title()}."
                    )
                meal_period = window.kind
        else:
            meal_period = None

        h3_index = latlng_to_h3(delivery_location.y, delivery_location.x)

        # --- Gate 1: OperationalZone check (Bypassed, matches order_placement.py) ---
        # op_zone = ZoneH3Index.objects.filter(
        #     h3_index=h3_index,
        #     resolution=ZONE_RESOLUTION,
        #     zone__is_active=True
        # ).select_related('zone').first()
        # if not op_zone:
        #     raise ValueError("Delivery location is not within an operational zone.")

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
        if drop_start:
            # The bucket must settle before the drop's cut-off, not the kitchen
            # release time, because the rider's trip is sized at cut-off.
            from domain.services.orders.drop_service import cutoff_at

            expires_at = cutoff_at(drop_window, drop_start.date())
        elif scheduled_for:
            # A scheduled bucket must be settled before the kitchen starts
            # prepping; an immediate bucket keeps the usual two-hour window.
            expires_at = release_at_for(restaurant, scheduled_for)
        else:
            expires_at = timezone.now() + timedelta(hours=2)

        with transaction.atomic():
            if drop_start:
                # Same serialisation as a solo drop order: lock the window so
                # two buckets cannot both claim the last seat.
                from apps.orders.models import DropWindow
                from domain.services.orders.drop_service import (
                    DropUnavailable,
                    validate_drop_booking,
                )

                DropWindow.objects.select_for_update().get(id=drop_window.id)
                try:
                    validate_drop_booking(drop_window, drop_start, restaurant=restaurant)
                except DropUnavailable as exc:
                    raise ValueError(str(exc))
            elif scheduled_for:
                # Take the same restaurant lock as the solo pre-order path so a
                # bucket and a solo order cannot both claim the last seat.
                locked_restaurant = Restaurant.objects.select_for_update().get(
                    id=restaurant.id,
                )
                ensure_preorder_slot_capacity(locked_restaurant, scheduled_for)

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
                            expires_at=expires_at,
                            scheduled_for=scheduled_for,
                            meal_period=meal_period,
                            drop_window=drop_window,
                        )
                        PoolMember.objects.create(pool=pool, user=initiator, status='ADDING')
                    break
                except IntegrityError as e:
                    if 'unique_open_pool_per_user' in str(e).lower() or 'initiator' in str(e).lower():
                        raise ValueError("You already have an active bucket.")
                    if i == max_retries - 1:
                        raise ValueError("Failed to generate a unique invite code.")
                    continue

        if pool:
            PoolOrderService._refresh_fee_preview(pool)
            broadcast_pool_status(str(pool.id), 'OPEN')
            notify_nearby_pool_created.delay(str(pool.id), restaurant.name)
        return pool

    @staticmethod
    def join_pool(user, invite_code):
        with transaction.atomic():
            pool = (
                OrderPool.objects.select_for_update()
                .select_related('drop_window', 'drop_window__drop_point')
                .get(invite_code=invite_code, status='OPEN')
            )

            if pool.expires_at <= timezone.now():
                raise ValueError("This pool has expired.")

            if PoolMember.objects.filter(user=user, pool__status='OPEN').exclude(pool=pool).exists():
                raise ValueError("You are already participating in another active bucket.")

            member_limit = PoolOrderService.MAX_POOL_MEMBERS
            if pool.drop_window_id:
                # A drop bucket is bounded by the trip: it can never carry more
                # members than the window has seats left, or the last members
                # would be sold a seat that does not exist.
                from domain.services.orders.drop_service import (
                    capacity as drop_capacity,
                    local_now,
                    reserved_seats,
                )

                window = pool.drop_window
                member_limit = min(member_limit, drop_capacity(window))
                seats = reserved_seats(
                    window, local_now(pool.scheduled_for).date(),
                )
                if seats >= drop_capacity(window):
                    raise ValueError("This drop is full — try the next window.")

            if pool.members.count() >= member_limit:
                raise ValueError(f"Pool is full (max {member_limit}).")

            member, created = PoolMember.objects.get_or_create(pool=pool, user=user, defaults={'status': 'ADDING'})

            if created:
                PoolOrderService._refresh_fee_preview(pool)
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

            if food.section.restaurant != pool.restaurant:
                raise ValueError("Food must be from the same restaurant as the pool.")

            # A pre-order bucket has a meal-scoped menu: members cannot add a
            # dinner dish to a lunch bucket, even if an old client shows it.
            if pool.meal_period and food.meal_period not in (
                pool.meal_period, Food.ALL_DAY,
            ):
                meal_label = dict(MealPeriod.KIND_CHOICES).get(
                    pool.meal_period, pool.meal_period.title(),
                )
                raise ValueError(
                    f"{food.name} is not part of the {meal_label.lower()} menu."
                )

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
            PoolOrderService._refresh_fee_preview(pool)

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
            PoolOrderService._refresh_fee_preview(pool)

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
            PoolOrderService._refresh_fee_preview(pool)
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
            if pool.expires_at <= timezone.now():
                raise ValueError("This pool has expired.")

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

            drop_fees = _drop_fee_shares(pool, members)
            if drop_fees:
                total_fee, s_fee, delivery_shares, service_shares = drop_fees
            else:
                total_fee, s_fee, _ = PricingService.calculate_delivery_fee(pool.restaurant, pool.delivery_location, pool=pool)
                subtotals = [m.items_subtotal for m in members]
                delivery_shares = _split_fee_proportionally(total_fee, subtotals)
                service_shares = _split_fee_proportionally(s_fee, subtotals)
            pool.total_delivery_fee = total_fee
            pool.service_fee = s_fee

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
    def finalize_pool(pool_id):
        """
        Creates individual Order records for each PoolMember that has paid.
        Called when all members have completed payment.
        """
        from decimal import Decimal

        from django.utils import timezone

        pool = OrderPool.objects.select_related('restaurant', 'drop_window').get(id=pool_id)

        if Order.objects.filter(pool=pool).exists():
            logger.info(f"[finalize_pool] Orders already exist for pool {pool_id}, skipping.")
            return

        members = pool.members.filter(status='PAID')
        if not members.exists():
            raise ValueError(f"No paid members found in pool {pool_id}.")

        # A scheduled bucket parks its orders in SCHEDULED so the per-minute
        # release sweep hands them to the kitchen at prep time. Immediate
        # buckets keep the confirmed-at-payment behaviour.
        scheduled_for = pool.scheduled_for
        release_at = (
            release_at_for(pool.restaurant, scheduled_for) if scheduled_for else None
        )

        # Transition pool LOCKED → PAID (required before READY)
        try:
            pool.status = PoolFSM.transition(pool.status, 'PAID')
        except InvalidPoolTransitionError:
            logger.warning(f"[finalize_pool] Pool {pool_id} status is '{pool.status}', expected 'LOCKED'")

        pool.paid_at = timezone.now()
        pool.save(update_fields=['status', 'paid_at'])

        orders = []
        order_items_bulk = []

        for member in members:
            items_data = member.items or []
            items_subtotal = member.items_subtotal or Decimal('0.00')
            delivery_fee = member.delivery_fee_share or Decimal('0.00')
            service_fee = member.service_fee_share or Decimal('0.00')
            total_amount = items_subtotal + delivery_fee + service_fee

            order = Order(
                customer=member.user,
                restaurant=pool.restaurant,
                pool=pool,
                is_pool_order=True,
                is_paid=True,
                status='SCHEDULED' if scheduled_for else 'CONFIRMED',
                scheduled_for=scheduled_for,
                release_at=release_at,
                items_subtotal=items_subtotal,
                delivery_fee=delivery_fee,
                service_fee=service_fee,
                total_amount=total_amount,
                delivery_address=pool.delivery_address,
                delivery_location=pool.delivery_location,
                h3_index=pool.h3_index,
                drop_window_id=pool.drop_window_id,
                drop_point_id=(
                    pool.drop_window.drop_point_id
                    if pool.drop_window_id else None
                ),
                group_breakdown={
                    'pool_id': str(pool.id),
                    'member_id': str(member.id),
                    'invite_code': pool.invite_code,
                },
                confirmed_at=None if scheduled_for else timezone.now(),
            )
            orders.append(order)

            for item in items_data:
                food_id = item.get('food_id')
                food = None
                if food_id:
                    try:
                        food = Food.objects.get(id=food_id)
                    except Food.DoesNotExist:
                        logger.warning(f"[finalize_pool] Food {food_id} not found for member {member.id}")
                order_items_bulk.append(
                    OrderItem(
                        order=order,
                        food=food,
                        quantity=item.get('quantity', 1),
                        price=Decimal(str(item.get('price', 0))),
                        section=food.section if food else None,
                    )
                )

        with transaction.atomic():
            Order.objects.bulk_create(orders)
            OrderItem.objects.bulk_create(order_items_bulk)

            _pool_id = str(pool.id)
            _message = (
                "All paid! Your group order is booked for later."
                if scheduled_for
                else "All paid! Restaurant is preparing."
            )
            transaction.on_commit(lambda: broadcast_pool_status(
                _pool_id, 'PAID', message=_message
            ))
            transaction.on_commit(lambda: EventBus.publish(PoolPaid(
                aggregate_id=pool.id,
                payload={'pool_id': _pool_id, 'all_paid': True},
            )))
            if not scheduled_for:
                # The kitchen is only told when a scheduled bucket is released.
                transaction.on_commit(lambda pid=_pool_id: _notify_pool_prep_sms(pid))

            for order in orders:
                _oid = str(order.id)
                _cid = str(order.customer.id)
                _pid = _pool_id
                transaction.on_commit(lambda oid=_oid, cid=_cid, pid=_pid: EventBus.publish(OrderCreated(
                    aggregate_id=oid,
                    payload={'customer_id': cid, 'pool_id': pid, 'is_pool': True},
                )))

        logger.info(f"[finalize_pool] Created {len(orders)} order(s) for pool {pool_id}")
        return orders

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

            if pool.scheduled_for:
                release_at = release_at_for(pool.restaurant, pool.scheduled_for)
                if timezone.now() < release_at:
                    raise ValueError(
                        "This bucket is booked for a later eating time; it "
                        "cannot be prepared before its release time."
                    )

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
    def decline_pool(pool_id: str, actor, reason: str = 'Pre-order declined by restaurant'):
        """
        The restaurant declines a paid pre-order bucket.

        Every member is refunded in full and their parked order is cancelled
        with a VENDOR fault, so each customer receives the pre-order-declined
        push while the reputation hit lands on the restaurant.
        """
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)

            if actor.role != 'admin' and not PoolOrderService._is_pool_restaurant_staff(actor, pool):
                raise PermissionError("Only the restaurant or an admin can decline this bucket.")

            if not pool.scheduled_for:
                raise ValueError("Only scheduled pre-order buckets can be declined.")

            # Declining is a paid-booking action: before payment the pool is
            # just an open invite, and cancel_pool already covers that case.
            if pool.status != 'PAID':
                raise ValueError(
                    f"Cannot decline bucket in {pool.status} state; "
                    "only paid buckets can be declined."
                )

            # A paid bucket stays PAID while its member orders move through
            # the kitchen, so the pool status alone cannot tell us whether
            # cooking has started. Refuse once any member order is being
            # prepared (or later): the food is already being made.
            cooking_started = pool.final_orders.filter(
                status__in=(
                    'PREPARING', 'READY', 'ASSIGNED', 'PICKED', 'DELIVERED',
                ),
            ).exists()
            if cooking_started:
                raise ValueError(
                    "Cannot decline a bucket after the kitchen has started preparing."
                )

            pool.status = PoolFSM.transition(pool.status, 'CANCELLED')
            pool.save(update_fields=['status'])

            from domain.services.payment_services import RefundPaymentService
            paid_members = list(pool.members.filter(status='PAID'))
            for member in paid_members:
                RefundPaymentService.execute('pool_member', member.id)
            PoolMember.objects.filter(
                id__in=[member.id for member in paid_members]
            ).update(status='REFUNDED')

            cancelled_orders = PoolOrderService._cancel_pool_orders(
                pool,
                actor,
                reason,
                category='vendor_rejected',
                fault='VENDOR',
                affects_restaurant_reputation=True,
            )

            _pool_id = str(pool.id)
            _actor_id = str(actor.id)
            _batch_id = str(pool.batch_id) if pool.batch_id else None
            transaction.on_commit(lambda: broadcast_pool_status(
                _pool_id, 'CANCELLED',
                message="The restaurant declined this bucket. Refunds are on the way.",
            ))
            transaction.on_commit(lambda: EventBus.publish(PoolCancelled(
                aggregate_id=pool.id,
                payload={
                    'pool_id': _pool_id,
                    'batch_id': _batch_id,
                    'actor_id': _actor_id,
                    'orders_cancelled': cancelled_orders,
                }
            )))
            return pool

    @staticmethod
    def _is_pool_restaurant_staff(actor, pool):
        if actor.role == 'vendor' and pool.restaurant.owner == actor:
            return True
        from apps.restaurants.models import SectionStaff
        return SectionStaff.objects.filter(
            user=actor, section__restaurant=pool.restaurant
        ).exists()

    @staticmethod
    def cancel_pool(pool_id, actor):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)

            is_restaurant = PoolOrderService._is_pool_restaurant_staff(actor, pool)
            if pool.initiator != actor and actor.role != 'admin' and not is_restaurant:
                raise PermissionError("Only the initiator, restaurant, or an admin can cancel the pool.")

            if not PoolFSM.can_cancel(pool.status):
                raise ValueError(f"Cannot cancel pool in {pool.status} state.")

            restaurant_cancelled = is_restaurant and pool.initiator != actor

            pool.status = PoolFSM.transition(pool.status, 'CANCELLED')
            pool.save(update_fields=['status'])

            from domain.services.payment_services import RefundPaymentService
            paid_members = list(pool.members.filter(status='PAID'))
            for member in paid_members:
                RefundPaymentService.execute('pool_member', member.id)
            PoolMember.objects.filter(
                id__in=[member.id for member in paid_members]
            ).update(status='REFUNDED')

            # Finalized member orders are not tied to a batch until the bucket
            # is dispatched, so cancelling the pool alone used to leave them
            # live (and their customers waiting on a meal nobody would cook).
            if restaurant_cancelled:
                cancelled_orders = PoolOrderService._cancel_pool_orders(
                    pool,
                    actor,
                    f'Pool {pool.id} was cancelled by the restaurant',
                    category='vendor_rejected',
                    fault='VENDOR',
                    affects_restaurant_reputation=True,
                )
            else:
                cancelled_orders = PoolOrderService._cancel_pool_orders(
                    pool,
                    actor,
                    f'Pool {pool.id} was cancelled',
                    category='pool_cancelled',
                )

            _pool_id = str(pool.id)
            _batch_id = str(pool.batch_id) if pool.batch_id else None
            _actor_id = str(actor.id)

            if _batch_id:
                transaction.on_commit(
                    lambda: PoolOrderService._cancel_associated_batch(_batch_id, _pool_id)
                )

            from apps.events.utils import broadcast_pool_status
            _message = (
                "Pool has been cancelled by the restaurant."
                if restaurant_cancelled
                else "Pool has been cancelled by the initiator."
            )
            transaction.on_commit(lambda: broadcast_pool_status(
                _pool_id, 'CANCELLED', message=_message
            ))
            transaction.on_commit(lambda: EventBus.publish(PoolCancelled(
                aggregate_id=pool.id,
                payload={
                    'pool_id': _pool_id,
                    'batch_id': _batch_id,
                    'actor_id': _actor_id,
                    'orders_cancelled': cancelled_orders,
                }
            )))
            return pool

    @staticmethod
    def _cancel_pool_orders(
        pool,
        actor,
        reason,
        *,
        category,
        fault=None,
        affects_restaurant_reputation=False,
    ):
        """
        Cancel every live order behind a cancelled bucket.

        This cannot reuse ``CancelOrderService``: that service authorises one
        order at a time, so the initiator would be rejected when cancelling
        another member's order. Refunds are issued once by the pool-level
        caller; this only rewrites state and publishes the per-order events
        that refresh customers' screens.
        """
        from domain.services.orders.order_cancellation import (
            _cancel_actor_type,
            _fault_for_actor,
        )

        cancelled_by = _cancel_actor_type(actor)
        resolved_fault = fault or _fault_for_actor(cancelled_by)
        order_ids = list(
            Order.objects.filter(
                pool=pool,
                status__in=CANCELLABLE_POOL_ORDER_STATUSES,
            ).values_list('id', flat=True)
        )
        if not order_ids:
            return 0

        Order.objects.filter(id__in=order_ids).update(
            status='CANCELLED',
            cancelled_at=timezone.now(),
            cancelled_by=cancelled_by,
            cancellation_fault=resolved_fault,
            cancellation_category=category,
            cancellation_reason=reason,
            affects_restaurant_reputation=affects_restaurant_reputation,
        )

        _pool_id = str(pool.id)
        _actor_id = str(actor.id)
        for order_id in order_ids:
            _order_id = str(order_id)
            transaction.on_commit(
                lambda oid=_order_id: EventBus.publish(OrderCancelled(
                    aggregate_id=oid,
                    payload={
                        'actor_id': _actor_id,
                        'pool_id': _pool_id,
                        'reason': reason,
                        'cancelled_by': cancelled_by,
                        'cancellation_fault': resolved_fault,
                        'cancellation_category': category,
                        'affects_restaurant_reputation': affects_restaurant_reputation,
                    },
                ))
            )
        return len(order_ids)

    @staticmethod
    def remove_member(pool_id, member_id, actor):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)
            if pool.initiator != actor and actor.role != 'admin':
                raise PermissionError("Only the initiator or an admin can remove members.")

            if pool.status not in ('OPEN', 'LOCKED'):
                raise ValueError("Can only remove members while pool is open or locked.")

            from apps.orders.models import PoolMember
            member = PoolMember.objects.select_for_update().get(id=member_id, pool_id=pool_id)
            if member.user == pool.initiator:
                raise ValueError("Cannot remove the pool initiator.")

            if member.status == 'PAID':
                from domain.services.payment_services import RefundPaymentService
                RefundPaymentService.execute('pool_member', member.id)

            member.delete()
            if pool.status == 'OPEN':
                PoolOrderService._refresh_fee_preview(pool)

            _pool_id = str(pool.id)
            from apps.events.utils import broadcast_pool_status
            transaction.on_commit(lambda: broadcast_pool_status(
                _pool_id, pool.status,
                message=f"{member.user.username} was removed from the pool."
            ))
            return pool

    @staticmethod
    def _cancel_associated_batch(batch_id: str, pool_id: str):
        from apps.orders.models import DeliveryBatch
        from domain.services.driver_free_service import DriverFreeService

        CANCELLABLE_STATES = {'ACCUMULATING', 'SOFT_LOCK', 'PENDING_DRIVER', 'DISPATCHED'}

        try:
            with transaction.atomic():
                batch = DeliveryBatch.objects.select_for_update().get(id=batch_id)
                if batch.status not in CANCELLABLE_STATES:
                    logger.warning(
                        f"[PoolOrderService] Batch {batch_id} is in '{batch.status}' "
                        f"— too late to cancel for pool {pool_id}. Manual intervention required."
                    )
                    return

                if batch.status == 'DISPATCHED' and batch.driver_id:
                    DriverFreeService.release(batch_id, event='CANCEL', reason=f'Pool {pool_id} was cancelled')
                    batch.refresh_from_db()

                batch.status = 'CANCELLED'
                batch.save(update_fields=['status', 'updated_at'])

                from apps.orders.models import Order
                Order.objects.filter(batch_id=batch_id).update(
                    status='CANCELLED',
                    cancelled_at=timezone.now(),
                    cancelled_by='SYSTEM',
                    cancellation_fault='SYSTEM',
                    cancellation_category='pool_cancelled',
                    cancellation_reason=f'Pool {pool_id} was cancelled',
                    affects_restaurant_reputation=False,
                )

                logger.info(f"[PoolOrderService] Cancelled batch {batch_id} (was {batch.status}) for pool {pool_id}")
        except DeliveryBatch.DoesNotExist:
            logger.warning(f"[PoolOrderService] Batch {batch_id} not found during pool cancellation")
        except Exception as exc:
            logger.error(f"[PoolOrderService] Failed to cancel batch {batch_id} for pool {pool_id}: {exc}", exc_info=True)
