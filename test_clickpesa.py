import os
import sys
import django

# Setup Django environment
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.conf import settings
from infrastructure.payment_gateways.factory import get_payment_gateway
from infrastructure.payment_gateways.clickpesa import ClickpesaGateway

def main():
    print("==================================================")
    print("ClickPesa Staging Integration Test Tool")
    print("==================================================")
    
    # Load configuration
    config = getattr(settings, 'CLICKPESA_CONFIG', {})
    client_id = config.get('CLIENT_ID')
    api_key = config.get('API_KEY')
    secret_key = config.get('SECRET_KEY')
    base_url = config.get('BASE_URL')
    
    print(f"Base URL:   {base_url}")
    print(f"Client ID:  {client_id}")
    print(f"API Key:    {api_key[:8]}..." if api_key else "API Key:    Not Set")
    print(f"Secret Key: {secret_key[:8]}..." if secret_key else "Secret Key: Not Set")
    print("--------------------------------------------------")
    
    if not client_id or not api_key or not secret_key:
        print("ERROR: ClickPesa configurations are missing in settings or environment variables.")
        print("Please define: CLICKPESA_CLIENT_ID, CLICKPESA_API_KEY, CLICKPESA_SECRET_KEY")
        sys.exit(1)
        
    gateway = ClickpesaGateway()
    
    print("Step 1: Testing Access Token Generation...")
    token = gateway._get_token()
    if token:
        print(f"SUCCESS: Generated JWT Token: Bearer {token[:20]}...")
    else:
        print("FAILED: Could not generate access token. Check credentials above.")
        sys.exit(1)
        
    print("\n--------------------------------------------------")
    print("Choose test option:")
    print("1. Test Payment Collection (USSD Push)")
    print("2. Test Manual Payout (Disbursement/Cashout)")
    print("3. Exit")
    choice = input("Enter choice (1-3): ").strip()
    
    if choice == '1':
        print("\n--- Test Payment Collection ---")
        phone = input("Enter customer phone number (e.g. 0712345678): ").strip()
        amount = input("Enter amount in TSh (e.g. 1000): ").strip()
        ref = input("Enter order reference (e.g. TEST_ORD_123): ").strip()
        
        res = gateway.collect(amount=amount, phone=phone, order_reference=ref)
        print("\nGateway Response:")
        print(res)
        
    elif choice == '2':
        print("\n--- Test Manual Payout ---")
        phone = input("Enter recipient phone number (e.g. 0712345678): ").strip()
        amount = input("Enter payout amount in TSh (e.g. 1000): ").strip()
        ref = input("Enter payout reference (e.g. TEST_CASHOUT_123): ").strip()
        
        res = gateway.disburse(amount=amount, account_info=phone, reference=ref)
        print("\nGateway Response:")
        print(res)
        
    else:
        print("Exiting.")

if __name__ == '__main__':
    main()
