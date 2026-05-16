import os
import requests
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv('RAFIKI_API_KEY', '')
BASE_URL = 'https://api.rafikisms.com'

def test_otp_flow():
    print("Testing OTP SMS...\n")

    # Test sending OTP message
    phone = "255761234567"
    otp = "123456"
    message = f"Your Orbit verification code is {otp}. It will expire in 5 minutes. Do not share this code with anyone."

    clean_phone = phone.strip().replace('+', '')
    if clean_phone.startswith('0'):
        clean_phone = '255' + clean_phone[1:]
    elif not clean_phone.startswith('255'):
        clean_phone = '255' + clean_phone

    url = f"{BASE_URL}/v1/vendor/send-sms"
    payload = {"phone": clean_phone, "message": message}
    headers = {"Content-Type": "application/json", "X-API-Key": API_KEY}

    response = requests.post(url, json=payload, headers=headers, timeout=10)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")

    if response.status_code in [200, 201]:
        print("\n✅ OTP SMS sent successfully!")
    else:
        print("\n❌ Failed to send OTP SMS")

if __name__ == "__main__":
    test_otp_flow()