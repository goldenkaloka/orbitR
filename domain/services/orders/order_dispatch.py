import logging
from django.db import transaction
from apps.orders.models import Order
from infrastructure.matching.strategies import PostgisRadiusStrategy
from domain.events.bus import EventBus
from domain.events.types import DispatchFailed

logger = logging.getLogger(__name__)

class DispatchService:
    @staticmethod
    def execute(order_id, is_retry=False, radius_km=15):
        """
        Core dispatch logic: Finding the best driver for an order using spatial matching.
        """
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            
            # 1. Check if already assigned
            if order.driver:
                logger.info(f"Order {order_id} already has a driver: {order.driver}")
                return order

            # 2. Identify search origin (Restaurant location)
            restaurant = order.restaurant
            if not restaurant.geo_location:
                logger.error(f"Cannot dispatch order {order_id}: Restaurant {restaurant.id} missing geo_location")
                return order

            # 3. Apply Matching Strategy
            matcher = PostgisRadiusStrategy()
            best_driver = matcher.find_best_driver(
                restaurant_lat=restaurant.geo_location.y,
                restaurant_lng=restaurant.geo_location.x,
                max_radius_km=radius_km,
                restaurant_id=restaurant.id
            )

            if best_driver:
                logger.info(f"Found driver {best_driver.id} for order {order_id}")
                from .order_assignment import OfferOrderService
                # Offer the order to the driver (leads to ACCEPT/REJECT flow)
                OfferOrderService.execute(order.id, best_driver.id, restaurant.owner)
            else:
                logger.warning(f"No drivers found for order {order_id} within {radius_km}km")
                
                # 4. Handle Failure / Retry
                if not is_retry:
                    from domain.services.dispatch_tasks import retry_dispatch_task
                    transaction.on_commit(lambda: retry_dispatch_task.delay(str(order.id), attempt=1))
                
                transaction.on_commit(lambda: EventBus.publish(DispatchFailed(
                    aggregate_id=order.id,
                    payload={'order_id': str(order.id), 'attempt_retry': not is_retry}
                )))

            return order
