from datetime import timedelta
from decimal import Decimal

from apps.marketplace.models import MarketplaceProduct
from django.db import transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import (
    MarketplaceProductActivated,
    MarketplaceProductCreated,
    MarketplaceProductExpired,
)


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
            product = MarketplaceProduct.objects.create(
                **validated_data
            )
            
            if images:
                from apps.marketplace.models import ProductImage
                for i, img in enumerate(images):
                    ProductImage.objects.create(
                        product=product,
                        image=img,
                        is_primary=(i == 0)
                    )
            
            # 2. Publish Event on Commit
            transaction.on_commit(lambda: EventBus.publish(MarketplaceProductCreated(
                aggregate_id=product.id,
                payload={
                    'seller_id': str(product.seller.id),
                    'price': float(product.price),
                    'fee_to_pay': float(product.calculate_listing_fee())
                }
            )))
        
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
        return count
