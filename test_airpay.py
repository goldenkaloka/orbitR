import os
import django
import json
import requests
import zlib
import sys
from decimal import Decimal

# Add current directory to path for standalone execution
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

try:
    # Setup Django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    from apps.orders.models import Order
    USE_DJANGO = True
except Exception as e:
    print(f"⚠️  Django Setup Warning: {str(e)}")
    print("Falling back to Standalone Mode (No database access).\n")
    USE_DJANGO = False
from infrastructure.payment_gateways.airpay import AirpayGateway
from infrastructure.payment_gateways.airpay_utils import AirpayTransformer
from django.conf import settings

def test_airpay_initiation():
    print("\n--- Testing Airpay Initiation ---")
    
    if USE_DJANGO:
        order = Order.objects.first()
        if not order:
            print("No order found in database. Using mock data.")
            order_id = "ORD-123"
            amount = Decimal("1500.00")
        else:
            order_id = str(order.id)
            amount = order.total_amount
    else:
        order_id = "ORD-MOCK-999"
        amount = Decimal("2500.00")
    
    gateway = AirpayGateway()
    print(f"Testing Order ID: {order_id}")
    print(f"Amount: {amount}")
    
    # Simulate the collect call
    result = gateway.collect(
        amount=amount,
        phone="255712345678",
        order_reference=order_id
    )
    
    print("\n[BACKEND RESPONSE TO FLUTTER]")
    print(json.dumps(result, indent=2))
    
    if result.get('success'):
        print("\n✅ Initiation Success!")
        print("These parameters would be passed to the Airpay Flutter SDK.")
    else:
        print("\n❌ Initiation Failed.")

def test_airpay_webhook_simulation():
    print("\n--- Testing Airpay Webhook Simulation ---")
    
    if USE_DJANGO:
        order = Order.objects.first()
        order_id = str(order.id) if order else "ORD-123"
        amount = str(int(order.total_amount)) if order else "1500"
    else:
        order_id = "ORD-MOCK-999"
        amount = "2500"

    gateway = AirpayGateway()
    
    # 1. Prepare raw response data
    payload_data = {
        "orderid": order_id,
        "ap_transactionid": "AP_TEST_9999",
        "amount": amount,
        "transaction_status": "200",
        "message": "Transaction Successful",
        "chmod": "Mpesa"
    }

    # 2. Calculate Secure Hash (CRC32)
    # format: orderid:ap_transactionid:amount:transaction_status:message:mercid:username
    hash_string = f"{payload_data['orderid']}:{payload_data['ap_transactionid']}:{payload_data['amount']}:{payload_data['transaction_status']}:{payload_data['message']}:{gateway.mercid}:{gateway.username}"
    secure_hash = zlib.crc32(hash_string.encode()) & 0xffffffff
    payload_data['ap_securehash'] = str(secure_hash)

    # 3. Encrypt the payload using Airpay logic
    # In Airpay v4, the entire JSON 'data' is encrypted
    full_payload = {"data": payload_data}
    json_payload = json.dumps(full_payload)
    
    encrypted_response = AirpayTransformer.encrypt_string(
        json_payload, 
        gateway.username, 
        gateway.password
    )

    print(f"\n[SIMULATED WEBHOOK PAYLOAD]")
    print(f"Encrypted 'response' field: {encrypted_response[:50]}...")

    # 4. Post to local webhook
    webhook_url = "http://localhost:8000/api/payments/airpay/callback/"
    print(f"Posting to: {webhook_url}")
    
    try:
        response = requests.post(webhook_url, json={"response": encrypted_response})
        print(f"Response Status: {response.status_code}")
        print(f"Response Body: {response.text}")
        
        if response.status_code == 200:
            print("\n✅ Webhook Processed Successfully!")
            if USE_DJANGO and order:
                order.refresh_from_db()
                print(f"New Order Status: {order.status}")
                print(f"Order Paid Flag: {order.is_paid}")
        else:
            print("\n❌ Webhook Processing Failed.")
    except Exception as e:
        print(f"Error calling webhook: {str(e)}")
        print("Note: Make sure your server is running (python manage.py runserver)")

if __name__ == "__main__":
    test_airpay_initiation()
    test_airpay_webhook_simulation()
