from django.db import transaction
import logging
from apps.core.geo_utils import get_neighbors

logger = logging.getLogger(__name__)

class BatchingService:
    """
    Service responsible for grouping separate orders into Delivery Batches
    based on Restaurant and H3 proximity.
    """
    
    @staticmethod
    def attempt_batching(restaurant_id, h3_index):
        """
        Check if there are multiple READY orders for a specific restaurant
        in nearby H3 cells and group them into a new DeliveryBatch.
        """
        from apps.orders.models import Order, DeliveryBatch
        
        with transaction.atomic():
            # Get current cell and immediate neighbors (1 ring = 7 cells total)
            neighbor_cells = get_neighbors(h3_index, 1)
            
            orders = list(Order.objects.select_for_update().filter(
                status='READY',
                restaurant_id=restaurant_id,
                h3_index__in=neighbor_cells,
                batch__isnull=True
            ))
            
            count = len(orders)
            
            if count > 1:
                logger.info(f"Creating batch for {count} orders at Restaurant {restaurant_id} -> H3 Cell {h3_index}")
                
                batch = DeliveryBatch.objects.create(
                    restaurant_id=restaurant_id,
                    status='CREATED'
                )
                
                for order in orders:
                    order.batch = batch
                    order.save()
                
                return batch
        
        return None
