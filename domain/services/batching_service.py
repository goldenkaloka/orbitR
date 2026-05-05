from django.db import transaction
from django.db.models import Count
import logging
import h3
from apps.core.geo_utils import get_neighbors

logger = logging.getLogger(__name__)

class BatchingService:
    """
    Service responsible for grouping separate orders into Delivery Batches
    based on Restaurant and DeliveryZone proximity.
    """
    
    @staticmethod
    def attempt_batching(restaurant_id, h3_index):
        """
        Check if there are multiple READY orders for a specific restaurant+zone
        and group them into a new DeliveryBatch.
        """
        from apps.orders.models import Order, DeliveryBatch
        
        with transaction.atomic():
            # Get current cell and immediate neighbors (1 ring = 7 cells total)
            neighbor_cells = get_neighbors(h3_index, 1)
            
            # 1. Evaluate queryset into a list immediately while holding the lock
            # This prevents inconsistent results between count() and iteration
            orders = list(Order.objects.select_for_update().filter(
                status='READY',
                restaurant_id=restaurant_id,
                h3_index__in=neighbor_cells,
                batch__isnull=True
            ))
            
            count = len(orders)
            
            # 2. We only batch if there's more than one order
            if count > 1:
                logger.info(f"Creating batch for {count} orders at Restaurant {restaurant_id} -> H3 Cell {h3_index}")
                
                # We no longer explicitly tie a batch to a specific DeliveryZone
                # as it spans an H3 cluster, but for compatibility we can link it
                # to the first order's zone.
                first_zone_id = orders[0].zone_id if orders[0].zone else None
                
                batch = DeliveryBatch.objects.create(
                    restaurant_id=restaurant_id,
                    zone_id=first_zone_id,
                    status='CREATED'
                )
                
                for order in orders:
                    order.batch = batch
                    order.save()
                
                return batch
        
        return None
