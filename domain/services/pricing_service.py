from decimal import Decimal
from apps.restaurants.models import DeliveryZone
import logging
import h3
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3

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
            delivery_h3 = latlng_to_h3(delivery_location.y, delivery_location.x, ZONE_RESOLUTION)
            
            # 1. Find the highest priority active zone that contains the delivery point
            zone = DeliveryZone.objects.filter(
                restaurant=restaurant,
                is_active=True,
                h3_index=delivery_h3
            ).order_by('-priority').first()

            if zone:
                logger.info(f"Matched Delivery Zone: {zone.name} with fee {zone.fee}")
                return zone.fee
            
            # 2. Fallback: Distance-based calculation using H3 grid distance
            if restaurant.geo_location and delivery_location:
                restaurant_h3 = latlng_to_h3(restaurant.geo_location.y, restaurant.geo_location.x, ZONE_RESOLUTION)
                try:
                    # distance in cells * ~170m per cell (at res 9) / 1000 to get km
                    distance_cells = h3.grid_distance(restaurant_h3, delivery_h3)
                    distance_km = distance_cells * 0.170
                except h3.H3ValueError:
                    distance_km = 10 # Fallback if too far to compute grid distance
                
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
