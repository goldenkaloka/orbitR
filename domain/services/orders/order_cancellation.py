import logging
from django.db import transaction
from apps.orders.models import Order
from apps.delivery.models import DeliveryAssignment
from domain.fsm.order_fsm import OrderFSM
from domain.fsm.driver_fsm import DriverFSM
from domain.events.bus import EventBus
from domain.events.types import OrderCancelled
from domain.services.payment_services import RefundPaymentService

logger = logging.getLogger(__name__)

class CancelOrderService:
    @staticmethod
    def execute(order_id, actor, reason='No reason provided'):
        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            
            # Role-based validation
            if actor.role == 'customer' and order.customer == actor:
                if order.status not in ['CREATED', 'CONFIRMED']:
                    raise PermissionError("Cannot cancel after preparation started.")
            elif actor.role == 'vendor' and order.restaurant.owner == actor:
                pass 
            elif actor.role == 'admin':
                pass
            else:
                raise PermissionError("No permission to cancel.")
            
            new_state = OrderFSM.transition(order.status, 'CANCELLED')
            order.status = new_state
            
            if order.is_paid:
                # If refund fails, we rollback the cancellation to keep state consistent
                # This ensures we don't have CANCELLED orders with COLLECTED payments
                RefundPaymentService.execute('order', order.id)

            if order.driver:
                if order.driver.status != 'AVAILABLE':
                    order.driver.status = DriverFSM.transition(order.driver.status, 'AVAILABLE')
                order.driver.current_active_orders = max(0, order.driver.current_active_orders - 1)
                order.driver.save(update_fields=['status', 'current_active_orders'])
                DeliveryAssignment.objects.filter(order=order).delete()
            
            order.save()
            
            transaction.on_commit(lambda: EventBus.publish(OrderCancelled(aggregate_id=order.id, payload={
                'actor_id': str(actor.id),
                'reason': reason
            })))
            return order
