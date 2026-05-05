from domain.events.bus import EventBus
from domain.events.types import MarketplaceProductCreated
from apps.marketplace.models import MarketplaceProduct
import logging
import h3
from apps.core.geo_utils import latlng_to_h3, ZONE_RESOLUTION

logger = logging.getLogger(__name__)

class MarketplaceEventHandler:
    @staticmethod
    def handle_product_created(event: MarketplaceProductCreated):
        """
        Asynchronously resolve the zone for a newly created product.
        """
        product_id = event.aggregate_id
        try:
            product = MarketplaceProduct.objects.get(id=product_id)
            if product.location and not product.h3_index:
                h3_index = latlng_to_h3(product.location.y, product.location.x, ZONE_RESOLUTION)
                if h3_index:
                    product.h3_index = h3_index
                    product.save(update_fields=['h3_index'])
                    logger.info(f"Resolved H3 index {h3_index} for Product {product.id} via EDA.")
        except MarketplaceProduct.DoesNotExist:
            logger.error(f"Product {product_id} not found during event handling.")

# Registration of handlers
def register_marketplace_handlers():
    EventBus.subscribe(MarketplaceProductCreated, MarketplaceEventHandler.handle_product_created)
