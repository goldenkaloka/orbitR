import requests
from decouple import config

def test_africastalking_sms():
    username = config('AFRICASTALKING_USERNAME', default='sandbox').strip()
    api_key = config('AFRICASTALKING_API_KEY', default='').strip()
    sender_id = config('AFRICASTALKING_SENDER_ID', default='').strip()
    
    # Must include + for Africa's Talking
    phone = "+255616622485" 
    message = "Test from Mwanachuo using Africa's Talking API!"

    if username == 'sandbox':
        url = "https://api.sandbox.africastalking.com/version1/messaging/bulk"
    else:
        url = "https://api.africastalking.com/version1/messaging/bulk"

    payload = {
        "username": username,
        "phoneNumbers": [phone],
        "message": message,
    }
    if sender_id:
        payload["senderId"] = sender_id

    headers = {
        "apiKey": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    print(f"Testing Africa's Talking Auth with Username: {username}")
    print(f"\nSending SMS to {url}...")
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=15)
        print(f"\nStatus Code: {response.status_code}")
        print(f"Response: {response.text}")
        
        if response.status_code in [200, 201]:
            print("\n✅ SUCCESS! Africa's Talking API works!")
        else:
            print("\n❌ FAILED. Check credentials or balance.")
            
    except Exception as e:
        print(f"\n🔥 ERROR: {str(e)}")

if __name__ == "__main__":
    test_africastalking_sms()
