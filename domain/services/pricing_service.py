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
            
            # 2. Fallback: Distance-based calculation
            # base_fee (500) + (distance_km * 100)
            if restaurant.geo_location and delivery_location:
                # distance() returns meters in geography mode
                distance_km = restaurant.geo_location.distance(delivery_location) / 1000
                base_fee = Decimal('500.00')
                per_km = Decimal('100.00')
                dynamic_fee = base_fee + (Decimal(str(distance_km)) * per_km)
                # Round to nearest 100 for simplicity in TSh
                return (dynamic_fee / 100).quantize(Decimal('1')) * 100
            
            logger.warning(f"No delivery zone or restaurant location found. Using default.")
            return cls.DEFAULT_FEE

        except Exception as e:
            logger.error(f"Error calculating zone-based fee: {str(e)}")
            return cls.DEFAULT_FEE
