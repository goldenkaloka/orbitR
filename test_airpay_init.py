import json
import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from apps.orders.models import Order
from infrastructure.payment_gateways.airpay import AirpayGateway


def test_init():
    order_id = '8d3f90df-6609-41bd-8b30-e7577ff2f7d0'
    order = Order.objects.get(id=order_id)
    
    gateway = AirpayGateway()
    # Force sandbox and NO MOCK for testing encryption logic
    gateway.sandbox = True
    gateway.mock = False
    
    # Ensure client_secret is set for test
    if not gateway.config.get('CLIENT_SECRET'):
        gateway.config['CLIENT_SECRET'] = gateway.config.get('SECRET_KEY')
    
    print(f"Initiating payment for Order: {order_id}, Amount: {order.total_amount}")
    
    result = gateway.collect(
        amount=order.total_amount,
        phone=order.customer.phone,
        order_reference=str(order.id),
        email=order.customer.email,
        firstname=order.customer.first_name,
        lastname=order.customer.last_name
    )
    
    print("\n--- Airpay Init Result ---")
    print(json.dumps(result, indent=2))
    print("---------------------------\n")
    
    if result.get('success'):
        if 'endpoint' in result:
            print(f"Endpoint: {result['endpoint']}")
            print("Params:")
            for k, v in result.get('params', {}).items():
                print(f"  {k}: {str(v)[:50]}..." if len(str(v)) > 50 else f"  {k}: {v}")
        else:
            print(f"Redirect URL: {result.get('redirect_url')}")

if __name__ == "__main__":
    test_init()
