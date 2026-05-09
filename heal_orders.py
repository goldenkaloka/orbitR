import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.orders.models import Order
from apps.delivery.models import DeliveryAssignment

def check_broken_orders():
    # Find orders that are ASSIGNED but have no driver in the Order model
    broken_orders = Order.objects.filter(status='ASSIGNED', driver__isnull=True)
    print(f"Found {broken_orders.count()} broken ASSIGNED orders (no driver).")
    
    for order in broken_orders:
        # Try to find a DeliveryAssignment for this order
        assignment = DeliveryAssignment.objects.filter(order=order, status='ACTIVE').first()
        if assignment:
            print(f"Order {order.id}: Found assignment to driver {assignment.driver_id}. Healing...")
            order.driver = assignment.driver
            order.save()
            print(f"Order {order.id}: Healed.")
        else:
            print(f"Order {order.id}: No ACTIVE assignment found. Resetting to READY.")
            order.status = 'READY'
            order.save()

if __name__ == "__main__":
    check_broken_orders()
