import logging
import random
import string
from decimal import Decimal
from datetime import timedelta
from django.db import transaction, IntegrityError
from django.utils import timezone
from apps.orders.models import Order, OrderItem, OrderPool, PoolMember, PoolItem
from apps.restaurants.models import Restaurant
from apps.geo.models import ZoneH3Index
from apps.events.utils import broadcast_pool_status
from domain.services.pricing_service import PricingService
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3
from domain.events.bus import EventBus
from domain.events.types import OrderCreated
import h3

logger = logging.getLogger(__name__)

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

        # --- Gate 2: Restaurant delivery radius check ---
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
                    PoolMember.objects.create(pool=pool, user=initiator)
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
            
            member, created = PoolMember.objects.get_or_create(pool=pool, user=user)
            
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

            pool_item, created = PoolItem.objects.get_or_create(
                member=member,
                food=food,
                defaults={'price_at_addition': food.price, 'quantity': 0}
            )
            
            pool_item.quantity += quantity
            pool_item.save()
            
            member.items_subtotal += (food.price * quantity)
            member.save()
            
            _pool_id, _username, _food_name = str(pool.id), user.username, food.name
            transaction.on_commit(lambda: broadcast_pool_status(_pool_id, 'OPEN', message=f"{_username} added {_food_name}"))
            return pool

    @staticmethod
    def remove_item(user, pool_id, item_id, quantity=1):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id, status='OPEN')
            member = PoolMember.objects.select_for_update().get(pool=pool, user=user)
            item = PoolItem.objects.select_for_update().get(id=item_id, member=member)
            
            remove_qty = min(quantity, item.quantity)
            price_delta = item.price_at_addition * remove_qty
            
            member.items_subtotal = max(Decimal('0.00'), member.items_subtotal - price_delta)
            member.save()
            
            food_name = item.food.name
            item.quantity -= remove_qty
            if item.quantity <= 0:
                item.delete()
            else:
                item.save()
            
            _pool_id, _username, _food_name = str(pool.id), user.username, food_name
            transaction.on_commit(lambda: broadcast_pool_status(_pool_id, 'OPEN', message=f"{_username} updated {_food_name}"))
            return pool

    @staticmethod
    def mark_member_ready(user, pool_id):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id, status='OPEN')
            if pool.expires_at <= timezone.now():
                raise ValueError("This pool has expired.")
            
            member = PoolMember.objects.select_for_update().get(pool=pool, user=user)
            if member.items_subtotal <= 0:
                raise ValueError("You must add at least one item.")
            
            member.status = 'READY'
            member.save()
            
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
            
            not_ready = pool.members.exclude(status='READY').values_list('user__username', flat=True)
            if not_ready.exists():
                raise ValueError(f"Members not ready: {', '.join(not_ready)}")
            
            members = list(pool.members.all())
            if not members:
                raise ValueError("Empty pool.")
            
            pool.status = 'LOCKED'
            total_fee, s_fee, _ = PricingService.calculate_delivery_fee(pool.restaurant, pool.delivery_location, pool=pool)
            pool.total_delivery_fee = total_fee
            pool.service_fee = s_fee
            
            n = len(members)
            d_base = total_fee // n
            d_rem = total_fee % n
            s_base = s_fee // n
            s_rem = s_fee % n
            
            for i, member in enumerate(members):
                d_extra = Decimal('1') if Decimal(str(i)) < d_rem else Decimal('0')
                s_extra = Decimal('1') if Decimal(str(i)) < s_rem else Decimal('0')
                member.delivery_fee_share = d_base + d_extra
                member.service_fee_share = s_base + s_extra
            
            PoolMember.objects.bulk_update(members, ['delivery_fee_share', 'service_fee_share'])
            pool.save()
            
            _pool_id = str(pool.id)
            transaction.on_commit(lambda: broadcast_pool_status(_pool_id, 'LOCKED', message="LOCKED! Time to pay."))
            return pool

    @staticmethod
    def finalize_pool(pool_id):
        with transaction.atomic():
            pool = OrderPool.objects.select_for_update().get(id=pool_id)
            if pool.status != 'LOCKED':
                raise ValueError("Pool is not LOCKED")
            
            if pool.members.filter(status__in=['JOINED', 'READY']).exists():
                raise ValueError("Not all members have paid")
            
            pool_h3 = latlng_to_h3(pool.delivery_location.y, pool.delivery_location.x, ZONE_RESOLUTION)
            
            master_order = Order.objects.create(
                customer=pool.initiator,
                restaurant=pool.restaurant,
                delivery_address=pool.delivery_address,
                delivery_location=pool.delivery_location,
                delivery_fee=pool.total_delivery_fee,
                delivery_otp=str(random.randint(1000, 9999)),
                pool=pool,
                h3_index=pool_h3,
                status='CREATED',
                is_paid=True
            )
            
            items_subtotal = Decimal('0.00')
            order_items = []
            breakdown = []
            
            for member in pool.members.all():
                m_items = []
                for item in member.items.all():
                    order_items.append(OrderItem(order=master_order, food=item.food, quantity=item.quantity, price=item.price_at_addition * item.quantity))
                    m_items.append({'food_name': item.food.name, 'quantity': item.quantity, 'price': float(item.price_at_addition)})
                    items_subtotal += (item.price_at_addition * item.quantity)
                
                breakdown.append({'username': member.user.username, 'phone': member.user.phone, 'items': m_items, 'subtotal': float(member.items_subtotal), 'delivery_share': float(member.delivery_fee_share)})
            
            OrderItem.objects.bulk_create(order_items)
            master_order.items_subtotal = items_subtotal
            master_order.total_amount = items_subtotal + pool.total_delivery_fee
            master_order.group_breakdown = breakdown
            master_order.save()
            
            pool.status = 'COMPLETED'
            pool.final_order = master_order
            pool.save()
            
            _pool_id = str(pool.id)
            transaction.on_commit(lambda: broadcast_pool_status(_pool_id, 'COMPLETED', message="Paid! Processing..."))
            
            transaction.on_commit(lambda: EventBus.publish(OrderCreated(aggregate_id=master_order.id, payload={'customer_id': str(master_order.customer.id), 'is_pool': True})))
            
            # Auto-confirm
            from .order_placement import ConfirmOrderService
            ConfirmOrderService.execute(master_order.id, master_order.customer)
            
            return master_order
    @staticmethod
    def cancel_pool(pool_id, actor):
        with transaction.atomic():
            from apps.orders.models import OrderPool
            pool = OrderPool.objects.select_for_update().get(id=pool_id)
            
            # Authorization check: only initiator or admin can cancel
            if pool.initiator != actor and actor.role != 'admin':
                raise PermissionError("Only the initiator or an admin can cancel the pool.")
            
            if pool.status not in ['OPEN', 'LOCKED']:
                raise ValueError(f"Cannot cancel pool in {pool.status} state.")
            
            pool.status = 'CANCELLED'
            pool.save()
            
            # Trigger refunds for all paid members
            from domain.services.payment_services import RefundPaymentService
            for member in pool.members.filter(status='PAID'):
                try:
                    RefundPaymentService.execute('pool_member', member.id)
                except Exception as e:
                    logger.error(f"Failed to refund pool member {member.id} during pool cancellation: {str(e)}")
            
            from apps.events.utils import broadcast_pool_status
            transaction.on_commit(lambda: broadcast_pool_status(str(pool.id), 'CANCELLED', message="Pool has been cancelled by the initiator."))
            return pool
