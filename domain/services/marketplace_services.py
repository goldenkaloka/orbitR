from datetime import timedelta
from decimal import Decimal

from apps.marketplace.models import MarketplaceProduct
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import (
    MarketplaceProductActivated,
    MarketplaceProductCreated,
    MarketplaceProductExpired,
)


def invalidate_product_list_cache():
    """
    Bump the cached product-list version.

    Every path that mutates listing state must call this after commit — the
    list view caches its response for 900s, so without a bump a seller's newly
    paid-for (or newly expired) listing stays invisible for up to 15 minutes.
    """
    try:
        cache.incr('marketplace_list_cache_version')
    except ValueError:
        cache.set('marketplace_list_cache_version', 1)


class MarketplaceService:
    """
    Domain service for Marketplace lifecycle management.
    Strictly follows DDD (Domain Logic) and EDA (Publishes Events).
    """

    @staticmethod
    def create_product(validated_data, images=None) -> MarketplaceProduct:
        """
        Create a new marketplace product and publish event.
        Includes idempotency check to prevent duplicate listings.
        """
        from datetime import timedelta

        from django.utils import timezone
        
        seller = validated_data.get('seller')
        name = validated_data.get('name')
        price = validated_data.get('price')
        
        # 1. Idempotency Check: Prevent duplicate creation within 5 mins
        recent_duplicate = MarketplaceProduct.objects.filter(
            seller=seller,
            name=name,
            price=price,
            created_at__gt=timezone.now() - timedelta(minutes=5)
        ).first()
        
        if recent_duplicate:
            return recent_duplicate

        with transaction.atomic():
            # Lock the seller record to prevent race conditions
            seller_locked = seller.__class__.objects.select_for_update().get(id=seller.id)
            from apps.marketplace.models import PostingTokenConfig, PostingTokenTransaction, ProductImage
            
            config = PostingTokenConfig.objects.first()
            deduction = config.token_deduction_per_listing if config else 1
            
            if seller_locked.posting_tokens_balance >= deduction:
                seller_locked.posting_tokens_balance -= deduction
                seller_locked.save(update_fields=['posting_tokens_balance'])
                
                validated_data['status'] = 'ACTIVE'
                validated_data['is_fee_paid'] = True
                validated_data['expiry_date'] = timezone.now() + timedelta(days=21)
                
                product = MarketplaceProduct.objects.create(**validated_data)
                
                # Record the transaction ledger entry
                PostingTokenTransaction.objects.create(
                    user=seller_locked,
                    transaction_type='LISTING_DEDUCTION',
                    amount=-deduction,
                    balance_after=seller_locked.posting_tokens_balance,
                    description=f"Deducted {deduction} token(s) for listing: {product.name}"
                )
                
                if images:
                    for i, img in enumerate(images):
                        ProductImage.objects.create(
                            product=product,
                            image=img,
                            is_primary=(i == 0)
                        )
                
                # Publish activation event on commit
                transaction.on_commit(lambda: EventBus.publish(MarketplaceProductActivated(
                    aggregate_id=product.id,
                    payload={
                        'activated_by': str(seller_locked.id),
                        'expiry_date': product.expiry_date.isoformat(),
                        'method': 'posting_token'
                    }
                )))
                transaction.on_commit(invalidate_product_list_cache)
            else:
                raise ValueError("You do not have enough posting tokens left. Please buy more.")
        
        return product

    @staticmethod
    def activate_product(product_id: str, actor) -> MarketplaceProduct:
        """
        Activate a product after listing fee payment.
        """
        from apps.payments.models import Payment
        with transaction.atomic():
            product = MarketplaceProduct.objects.select_for_update().get(id=product_id)
            
            if product.seller != actor and not actor.is_staff:
                raise PermissionError("Only the seller can activate this product.")
                
            if product.status == 'ACTIVE':
                return product

            product.status = 'ACTIVE'
            product.is_fee_paid = True
            product.expiry_date = timezone.now() + timedelta(days=21)
            product.save()
            
            transaction.on_commit(lambda: EventBus.publish(MarketplaceProductActivated(
                aggregate_id=product.id,
                payload={'activated_by': str(actor.id), 'expiry_date': product.expiry_date.isoformat()}
            )))
            transaction.on_commit(invalidate_product_list_cache)
        
        return product

    @staticmethod
    def reactivate_expired_product(product_id: str, actor) -> MarketplaceProduct:
        """
        Extend an expired product for another 21 days after 1% fee payment.
        """
        from apps.payments.models import Payment
        with transaction.atomic():
            product = MarketplaceProduct.objects.select_for_update().get(id=product_id)
            
            if product.seller != actor:
                raise PermissionError("Only the seller can reactivate this product.")
            
            product.status = 'ACTIVE'
            product.expiry_date = timezone.now() + timedelta(days=21)
            product.save()
            
            transaction.on_commit(lambda: EventBus.publish(MarketplaceProductActivated(
                aggregate_id=product.id,
                payload={'action': 'reactivation', 'expiry_date': product.expiry_date.isoformat()}
            )))
            transaction.on_commit(invalidate_product_list_cache)
        
        return product

    @staticmethod
    def mark_expired_products():
        """
        Background task logic to mark products as expired.
        """
        expired_qs = MarketplaceProduct.objects.filter(
            status='ACTIVE',
            expiry_date__lte=timezone.now()
        )
        
        count = 0
        for product in expired_qs:
            product.status = 'EXPIRED'
            product.save()
            EventBus.publish(MarketplaceProductExpired(
                aggregate_id=product.id,
                payload={'seller_id': str(product.seller.id)}
            ))
            count += 1
        if count:
            invalidate_product_list_cache()
        return count
