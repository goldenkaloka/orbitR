from django.db import transaction
from django.db.models import Count
import logging

logger = logging.getLogger(__name__)

class BatchingService:
    """
    Service responsible for grouping separate orders into Delivery Batches
    based on Restaurant and DeliveryZone proximity.
    """
    
    @staticmethod
    def attempt_batching(restaurant_id, zone_id):
        """
        Check if there are multiple READY orders for a specific restaurant+zone
        and group them into a new DeliveryBatch.
        """
        from apps.orders.models import Order, DeliveryBatch
        
        with transaction.atomic():
            # 1. Find READY orders for this combo that aren't already in a batch
            orders_to_batch = Order.objects.select_for_update().filter(
                status='READY',
                restaurant_id=restaurant_id,
                zone_id=zone_id,
                batch__isnull=True
            )
            
            count = orders_to_batch.count()
            
            # 2. We only batch if there's more than one order
            if count > 1:
                logger.info(f"Creating batch for {count} orders at Restaurant {restaurant_id} -> Zone {zone_id}")
                
                batch = DeliveryBatch.objects.create(
                    restaurant_id=restaurant_id,
                    zone_id=zone_id,
                    status='CREATED'
                )
                
                for order in orders_to_batch:
                    order.batch = batch
                    order.save()
                
                return batch
        
        return None
