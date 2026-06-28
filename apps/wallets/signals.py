from decimal import Decimal

from apps.orders.models import Order
from apps.users.models import User
from django.core.cache import cache
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Wallet, WalletTransaction


@receiver(post_save, sender=User)
def create_user_wallet(sender, instance, created, **kwargs):
    if created:
        Wallet.objects.get_or_create(user=instance)

@receiver(post_save, sender=Order)
def handle_order_wallet_updates(sender, instance, created, **kwargs):
    """
    Event-driven wallet updates based on order status.
    - DELIVERED: Standard success scenario.
    - CANCELLED: Refund logic (if paid).
    - DISPUTED: Special logic for restaurant mistakes.
    """
    if instance.status in ['DELIVERED', 'DISPUTED'] and not instance.is_settled:
        with transaction.atomic():
            Order.objects.filter(id=instance.id).update(is_settled=True)
            # Restaurant gets nothing (Refunded to customer via separate process)

@receiver(post_save, sender=Wallet)
def invalidate_stats_cache_on_wallet_change(sender, instance, **kwargs):
    """Invalidate the user stats cache when wallet balance changes."""
    if instance.user:
        cache.delete(f"user_stats_{instance.user.id}_{instance.user.role}")
    if instance.section_id:
        from apps.restaurants.models import SectionStaff
        staff_user_ids = SectionStaff.objects.filter(
            section_id=instance.section_id
        ).values_list('user_id', flat=True)
        for uid in staff_user_ids:
            cache.delete(f"user_stats_{uid}_vendor")
