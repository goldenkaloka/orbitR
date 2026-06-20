import logging
from uuid import UUID

from apps.marketplace.models import BuyerDemand, SellerOffer, Shop
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from django.db import transaction
from django.utils import timezone
from domain.events.bus import EventBus
from domain.events.types import (
    BuyerDemandAccepted,
    BuyerDemandCancelled,
    BuyerDemandCreated,
    BuyerDemandExpired,
    SellerOfferCreated,
)

logger = logging.getLogger(__name__)

DEMAND_EXPIRY_SECONDS = 300


class DemandService:

    @staticmethod
    def create_demand(buyer_id: UUID, title: str, lat: float, lng: float,
                      radius_km: int = 5, description: str = None,
                      category_ids: list = None) -> BuyerDemand:
        from django.contrib.auth import get_user_model
        User = get_user_model()

        demand = BuyerDemand(
            buyer_id=buyer_id,
            title=title,
            description=description,
            location=Point(lng, lat, srid=4326),
            radius_km=radius_km,
            status='OPEN',
        )
        demand.save()

        if category_ids:
            from apps.marketplace.models import MarketplaceCategory
            categories = MarketplaceCategory.objects.filter(id__in=category_ids)
            demand.categories.set(categories)

        EventBus.publish(BuyerDemandCreated(
            aggregate_id=demand.id,
            payload={
                'demand_id': str(demand.id),
                'buyer_id': str(buyer_id),
                'title': title,
                'lat': lat,
                'lng': lng,
                'h3_index': demand.h3_index or '',
                'radius_km': radius_km,
                'expires_at': str(demand.expires_at),
                'category_ids': category_ids or [],
            }
        ))

        from apps.marketplace.tasks import expire_demand
        expire_demand.apply_async(args=[str(demand.id)], countdown=DEMAND_EXPIRY_SECONDS)

        return demand

    @staticmethod
    @transaction.atomic
    def create_offer(demand_id: UUID, seller_id: UUID, price: float,
                     shop_id: UUID = None, availability: bool = True,
                     message: str = None) -> SellerOffer:
        demand = BuyerDemand.objects.select_for_update().get(id=demand_id)

        if demand.status != 'OPEN':
            raise ValueError(f"Demand is {demand.status}, not OPEN")
        if demand.is_expired():
            demand.status = 'EXPIRED'
            demand.save(update_fields=['status'])
            raise ValueError("Demand has expired")

        offer = SellerOffer(
            demand_id=demand_id,
            seller_id=seller_id,
            shop_id=shop_id,
            price=price,
            availability=availability,
            message=message,
        )
        offer.save()

        EventBus.publish(SellerOfferCreated(
            aggregate_id=offer.id,
            payload={
                'offer_id': str(offer.id),
                'demand_id': str(demand_id),
                'seller_id': str(seller_id),
                'price': str(price),
                'shop_id': str(shop_id) if shop_id else None,
            }
        ))

        return offer

    @staticmethod
    def accept_offer(demand_id: UUID, offer_id: UUID, buyer_id: UUID) -> dict:
        with transaction.atomic():
            demand = BuyerDemand.objects.select_for_update().select_related('buyer').get(id=demand_id)

            if str(demand.buyer_id) != str(buyer_id):
                raise ValueError("Only the demand creator can accept offers")
            if demand.status != 'OPEN':
                raise ValueError(f"Demand is {demand.status}")

            offer = SellerOffer.objects.select_for_update().select_related('seller', 'shop').get(
                id=offer_id, demand_id=demand_id
            )

            if offer.status != 'PENDING':
                raise ValueError(f"Offer is {offer.status}")

            demand.status = 'ACCEPTED'
            demand.accepted_offer = offer
            demand.save(update_fields=['status', 'accepted_offer'])

            offer.status = 'ACCEPTED'
            offer.save(update_fields=['status'])

            SellerOffer.objects.filter(
                demand_id=demand_id, status='PENDING'
            ).exclude(id=offer_id).update(status='DECLINED')

        buyer_info = {
            'buyer_username': demand.buyer.username,
            'buyer_phone': demand.buyer.phone,
            'buyer_lat': demand.location.y,
            'buyer_lng': demand.location.x,
        }
        seller_info = {
            'seller_username': offer.seller.username,
            'seller_phone': offer.seller.phone,
            'shop_name': offer.shop.name if offer.shop else None,
            'shop_lat': offer.shop.location.y if offer.shop and offer.shop.location else None,
            'shop_lng': offer.shop.location.x if offer.shop and offer.shop.location else None,
            'offer_price': str(offer.price),
        }

        EventBus.publish(BuyerDemandAccepted(
            aggregate_id=demand.id,
            payload={
                'demand_id': str(demand.id),
                'offer_id': str(offer.id),
                'buyer_id': str(buyer_id),
                'seller_id': str(offer.seller_id),
                'title': demand.title,
                'buyer_info': buyer_info,
                'seller_info': seller_info,
            }
        ))

        return {
            'demand': demand,
            'offer': offer,
            'buyer_info': buyer_info,
            'seller_info': seller_info,
        }

    @staticmethod
    @transaction.atomic
    def expire_demand(demand_id: UUID):
        try:
            demand = BuyerDemand.objects.get(id=demand_id)
        except BuyerDemand.DoesNotExist:
            return

        if demand.status != 'OPEN':
            return

        demand.status = 'EXPIRED'
        demand.save(update_fields=['status'])

        SellerOffer.objects.filter(demand_id=demand_id, status='PENDING').update(
            status='DECLINED'
        )

        EventBus.publish(BuyerDemandExpired(
            aggregate_id=demand.id,
            payload={
                'demand_id': str(demand.id),
                'buyer_id': str(demand.buyer_id),
                'title': demand.title,
            }
        ))

    @staticmethod
    @transaction.atomic
    def cancel_demand(demand_id: UUID, buyer_id: UUID):
        demand = BuyerDemand.objects.select_for_update().get(id=demand_id)

        if str(demand.buyer_id) != str(buyer_id):
            raise ValueError("Only the demand creator can cancel")
        if demand.status != 'OPEN':
            raise ValueError(f"Demand is {demand.status}")

        demand.status = 'CANCELLED'
        demand.save(update_fields=['status'])

        SellerOffer.objects.filter(demand_id=demand_id, status='PENDING').update(
            status='DECLINED'
        )

        EventBus.publish(BuyerDemandCancelled(
            aggregate_id=demand.id,
            payload={
                'demand_id': str(demand.id),
                'buyer_id': str(buyer_id),
                'title': demand.title,
            }
        ))

        return demand
