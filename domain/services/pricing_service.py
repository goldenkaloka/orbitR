from decimal import Decimal
from apps.restaurants.models import DeliveryZone
import logging

logger = logging.getLogger(__name__)

class PricingService:
    """
    Business logic for calculating delivery fees based on geographic zones.
    """
    
    DEFAULT_FEE = Decimal('500.00')

    @classmethod
    def calculate_delivery_fee(cls, restaurant, delivery_location):
        """
        Calculate dynamic delivery fee based on PostGIS zones.
        """
        if not delivery_location or not restaurant:
            return cls.DEFAULT_FEE

        try:
            # 1. Find the highest priority active zone that contains the delivery point
            # Uses PostGIS boundary__contains=point
            zone = DeliveryZone.objects.filter(
                restaurant=restaurant,
                is_active=True,
                boundary__contains=delivery_location
            ).order_by('-priority').first()

            if zone:
                logger.info(f"Matched Delivery Zone: {zone.name} with fee {zone.fee}")
                return zone.fee
            
            # 2. Fallback: If no zone is matched, you can either:
            # - Return a high "Out of Zone" fee
            # - Return a default fee
            # - Or raise an error to block the order (uncomment below if desired)
            # raise ValueError("Delivery location is outside the restaurant's supported zones.")
            
            logger.warning(f"No delivery zone found for restaurant {restaurant.id} at {delivery_location}. Using default.")
            return cls.DEFAULT_FEE

        except Exception as e:
            logger.error(f"Error calculating zone-based fee: {str(e)}")
            return cls.DEFAULT_FEE
