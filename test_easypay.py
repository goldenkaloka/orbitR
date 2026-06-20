import os
import sys

import django

# Setup Django environment
sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.conf import settings
from infrastructure.payment_gateways.factory import get_payment_gateway


def test_easypay_init():
    print("🚀 Initializing EasyPay Gateway Test...")
    
    # Force use real gateway for testing
    settings.EASYPAY_CONFIG['MOCK'] = False
    
    gateway = get_payment_gateway()
    
    print(f"\nGateway Type: {type(gateway).__name__}")
    print(f"API Key (masked): {settings.EASYPAY_CONFIG.get('CLIENT_SECRET')[:10]}...")
    
    # Test collect with a dummy number
    # EasyPay expects 255... format
    test_phone = "255700000000"
    test_amount = 100.0
    test_ref = "TEST_EP_001"
    
    print(f"\nAttempting to initiate EasyPay USSD Push...")
    print(f"Phone: {test_phone}")
    print(f"Amount: {test_amount}")
    
    response = gateway.collect(
        amount=test_amount,
        phone=test_phone,
        order_reference=test_ref
    )
    
    print("\n--- EasyPay Result ---")
    if response.get('success'):
        print("✅ SUCCESS: Payment initiated.")
        print(f"Message: {response.get('params', {}).get('message')}")
        print(f"EasyPay Raw: {response.get('params', {}).get('easypay_response')}")
    else:
        print("❌ FAILED: Could not initiate payment.")
        print(f"Error: {response.get('error')}")

if __name__ == "__main__":
    test_easypay_init()
