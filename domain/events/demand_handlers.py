import logging
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.geos import Point
from django.contrib.gis.measure import D
from domain.events.bus import EventBus
from domain.events.types import (
    BuyerDemandCreated, BuyerDemandExpired,
    BuyerDemandAccepted, SellerOfferCreated,
    BuyerDemandCancelled,
)
from apps.events.utils import (
    broadcast_demand_update, broadcast_new_demand_to_sellers,
)
from infrastructure.notifications.service import NotificationService
from infrastructure.notifications.messages import (
    get_demand_message, get_offer_message,
    get_demand_accepted_message_for_buyer,
    get_demand_cancelled_message,
)

logger = logging.getLogger(__name__)


def on_demand_created(event: BuyerDemandCreated):
    payload = event.payload
    demand_id = payload['demand_id']
    lat = payload['lat']
    lng = payload['lng']
    h3_index = payload.get('h3_index', '')
    radius_km = payload['radius_km']

    logger.info(f"Demand {demand_id} created — finding nearby shops")

    from apps.marketplace.models import Shop

    point = Point(lng, lat, srid=4326)
    nearby_shops = Shop.objects.filter(
        location__dwithin=(point, D(km=radius_km)),
        is_active=True,
    ).annotate(
        distance=Distance('location', point),
    ).only('owner_id').order_by('distance')

    seller_ids = list(nearby_shops.values_list('owner_id', flat=True).distinct())

    if not seller_ids:
        logger.info(f"No nearby sellers found for demand {demand_id}")
        broadcast_demand_update(demand_id, 'no_sellers_nearby')
        return

    logger.info(f"Broadcasting demand {demand_id} to {len(seller_ids)} sellers")

    broadcast_new_demand_to_sellers(
        demand_id=demand_id,
        seller_ids=[str(s) for s in seller_ids],
        demand_title=payload['title'],
        buyer_h3=h3_index,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
    )

    title, body = get_demand_message(payload['title'], radius_km)
    for seller_id in seller_ids:
        try:
            NotificationService.send_push(
                user_id=str(seller_id),
                title=title,
                body=body,
                data={
                    'type': 'new_demand',
                    'demand_id': str(demand_id),
                    'title': payload['title'],
                },
            )
        except Exception as e:
            logger.warning(f"Push failed for seller {seller_id}: {e}")


def on_offer_created(event: SellerOfferCreated):
    payload = event.payload
    demand_id = payload['demand_id']
    logger.info(f"New offer {event.aggregate_id} on demand {demand_id}")

    broadcast_demand_update(
        demand_id,
        'new_offer',
        extra={
            'offer_id': payload['offer_id'],
            'seller_id': payload['seller_id'],
            'price': payload['price'],
        }
    )

    from apps.marketplace.models import BuyerDemand

    title, body = get_offer_message()
    try:
        demand = BuyerDemand.objects.get(id=demand_id)
        NotificationService.send_push(
            user_id=str(demand.buyer_id),
            title=title,
            body=body,
            data={
                'type': 'new_offer',
                'demand_id': str(demand_id),
                'offer_id': payload['offer_id'],
            },
        )
    except Exception as e:
        logger.warning(f"Push to buyer failed for demand {demand_id}: {e}")


def on_demand_accepted(event: BuyerDemandAccepted):
    payload = event.payload
    demand_id = payload['demand_id']
    offer_id = payload['offer_id']

    logger.info(f"Demand {demand_id} accepted — offer {offer_id}")

    broadcast_demand_update(
        demand_id,
        'demand_accepted',
        extra={
            'offer_id': offer_id,
            'buyer_info': payload['buyer_info'],
            'seller_info': payload['seller_info'],
        }
    )

    buyer = payload['buyer_info']
    seller = payload['seller_info']
    title = "Offer yako imekubaliwa!"
    body = f"{buyer['buyer_username']} amekubali ofa yako ya TSh {seller['offer_price']} kwa {payload['title']}"

    NotificationService.send_push(
        user_id=payload['seller_id'],
        title=title,
        body=body,
        data={
            'type': 'offer_accepted',
            'demand_id': str(demand_id),
            'offer_id': str(offer_id),
            'buyer_info': buyer,
            'seller_info': seller,
        },
    )

    from apps.marketplace.models import SellerOffer

    declined_offers = SellerOffer.objects.filter(
        demand_id=demand_id, status='DECLINED'
    ).select_related('seller')

    declined_title, declined_body = get_demand_accepted_message_for_buyer(
        payload['title'], seller['shop_name'] or seller['seller_username']
    )
    for offer in declined_offers:
        try:
            NotificationService.send_push(
                user_id=str(offer.seller_id),
                title=declined_title,
                body=declined_body,
                data={
                    'type': 'offer_declined',
                    'demand_id': str(demand_id),
                },
            )
        except Exception as e:
            logger.warning(f"Push to losing seller {offer.seller_id} failed: {e}")


def on_demand_expired(event: BuyerDemandExpired):
    payload = event.payload
    demand_id = payload['demand_id']

    logger.info(f"Demand {demand_id} expired")

    broadcast_demand_update(demand_id, 'demand_expired')

    title = "Ombi lako limeisha"
    body = f"Ombi lako la '{payload['title']}' limeisha bila kupata ofa zilizokubaliwa."

    NotificationService.send_push(
        user_id=payload['buyer_id'],
        title=title,
        body=body,
        data={
            'type': 'demand_expired',
            'demand_id': str(demand_id),
        },
    )


def on_demand_cancelled(event: BuyerDemandCancelled):
    payload = event.payload
    demand_id = payload['demand_id']

    logger.info(f"Demand {demand_id} cancelled")

    broadcast_demand_update(demand_id, 'demand_cancelled')

    from apps.marketplace.models import SellerOffer

    title, body = get_demand_cancelled_message(payload['title'])
    declined_offers = SellerOffer.objects.filter(
        demand_id=demand_id, status='DECLINED'
    ).select_related('seller')
    for offer in declined_offers:
        try:
            NotificationService.send_push(
                user_id=str(offer.seller_id),
                title=title,
                body=body,
                data={
                    'type': 'demand_cancelled',
                    'demand_id': str(demand_id),
                },
            )
        except Exception as e:
            logger.warning(f"Push failed for seller {offer.seller_id}: {e}")


def register_demand_handlers():
    EventBus.subscribe(BuyerDemandCreated, on_demand_created)
    EventBus.subscribe(SellerOfferCreated, on_offer_created)
    EventBus.subscribe(BuyerDemandAccepted, on_demand_accepted)
    EventBus.subscribe(BuyerDemandExpired, on_demand_expired)
    EventBus.subscribe(BuyerDemandCancelled, on_demand_cancelled)
