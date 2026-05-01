import os
import django
import sys

# Setup Django environment so we can use your project's settings and gateway code
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from infrastructure.payment_gateways.airpay import AirpayGateway

def test_airpay_auth():
    print("🚀 Initializing Airpay Gateway Test...")
    
    # We will temporarily force MOCK=False just for this test
    # so we hit the real API regardless of your .env
    from django.conf import settings
    settings.AIRPAY_CONFIG['MOCK'] = False
    
    gateway = AirpayGateway()
    
    print("\nAttempting Authentication with Airpay...")
    print(f"Merchant ID: {settings.AIRPAY_CONFIG.get('MERCHANT_ID')}")
    print(f"Username: {settings.AIRPAY_CONFIG.get('USERNAME')}")
    print(f"URL: {gateway.base_url}/getAccessToken")
    print("-" * 50)
    
    try:
        # get_auth_token() calls the Airpay API to verify credentials and get an access token
        token = gateway.get_auth_token()
        
        if token:
            print("\n✅ SUCCESS! Authentication Passed.")
            print(f"Access Token Received: {token[:20]}...[truncated]")
        else:
            print("\n❌ FAILED. Could not get access token. Check your credentials.")
    except Exception as e:
        print(f"\n🔥 EXCEPTION OCCURRED: {str(e)}")
        print("Check if the URL is correct or if your IP needs to be whitelisted by Airpay.")

if __name__ == "__main__":
    test_airpay_auth()
