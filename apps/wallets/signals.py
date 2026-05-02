from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.users.models import User
from apps.orders.models import Order
from .models import Wallet, WalletTransaction
from django.db import transaction
from decimal import Decimal

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
    if instance.status == 'DELIVERED' and not instance.is_settled:
        # Use a transaction to ensure atomicity
        with transaction.atomic():
            # Mark as settled first to prevent race conditions
            Order.objects.filter(id=instance.id).update(is_settled=True)
            
            # 1. Update Vendor Wallet (Standard: Subtotal - Platform Fee)
            if instance.restaurant and instance.restaurant.owner:
                vendor_wallet, _ = Wallet.objects.get_or_create(user=instance.restaurant.owner)
                platform_fee_pct = Decimal('0.10') # 10% Platform Fee
                vendor_share = instance.items_subtotal * (Decimal('1.00') - platform_fee_pct)
                
                vendor_wallet.balance += vendor_share
                vendor_wallet.save()
                
                WalletTransaction.objects.create(
                    wallet=vendor_wallet,
                    amount=vendor_share,
                    tx_type='CREDIT',
                    description=f"Earnings from Order {instance.id}",
                    order=instance
                )

            # 2. Update Driver Wallet (Standard: 100% Delivery Fee)
            if instance.driver and instance.driver.user:
                driver_wallet, _ = Wallet.objects.get_or_create(user=instance.driver.user)
                driver_share = instance.delivery_fee
                
                driver_wallet.balance += driver_share
                driver_wallet.save()
                
                WalletTransaction.objects.create(
                    wallet=driver_wallet,
                    amount=driver_share,
                    tx_type='CREDIT',
                    description=f"Delivery fee for Order {instance.id}",
                    order=instance
                )

    elif instance.status == 'DISPUTED' and not instance.is_settled:
        # Scenario 4: Restaurant Mistake, but Driver gets paid
        with transaction.atomic():
            Order.objects.filter(id=instance.id).update(is_settled=True)
            # Only Pay the Driver
            if instance.driver and instance.driver.user:
                driver_wallet, _ = Wallet.objects.get_or_create(user=instance.driver.user)
                driver_wallet.balance += instance.delivery_fee
                driver_wallet.save()
                
                WalletTransaction.objects.create(
                    wallet=driver_wallet,
                    amount=instance.delivery_fee,
                    tx_type='CREDIT',
                    description=f"Delivery fee for Disputed Order {instance.id} (Fuel Coverage)",
                    order=instance
                )
            # Restaurant gets nothing (Refunded to customer via separate process)
