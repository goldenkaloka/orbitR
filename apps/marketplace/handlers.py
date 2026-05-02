from domain.events.bus import EventBus
from domain.events.types import MarketplaceProductCreated
from domain.services.geo_services import GeoDomainService
from apps.marketplace.models import MarketplaceProduct
import logging

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
            if product.location and not product.zone:
                zone = GeoDomainService.find_zone_for_point(product.location.x, product.location.y)
                if zone:
                    product.zone = zone
                    product.save(update_fields=['zone'])
                    logger.info(f"Resolved zone {zone.name} for Product {product.id} via EDA.")
        except MarketplaceProduct.DoesNotExist:
            logger.error(f"Product {product_id} not found during event handling.")

# Registration of handlers
def register_marketplace_handlers():
    EventBus.subscribe(MarketplaceProductCreated, MarketplaceEventHandler.handle_product_created)
