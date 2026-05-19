import json
import requests

# 1. Configuration (extracted from your .env)
API_KEY = "sk_qUvgWy4fU1btBrmEnQM2ggYr5IZieRMU0YvOrhWbdo5KVKxB"
SENDER_NAME = "Orbit"  # Approved sender ID on your account
PHONE_NUMBER = "255616622485"  # Ensure international format without '+'

# 2. Setup Payload & Headers
url = "https://api.rafikisms.com/v1/vendor/send-sms"
payload = {
    "phone": PHONE_NUMBER,
    "message": "Hello from Orbit! This is a test SMS.",
    "sender_id": SENDER_NAME,
    "sender": SENDER_NAME
}
headers = {
    "Content-Type": "application/json",
    "X-API-Key": API_KEY
}

# 3. Dispatch Request
print(f"Sending test SMS to {PHONE_NUMBER} via {url}...")
try:
    response = requests.post(url, json=payload, headers=headers, timeout=10)
    print(f"\n[HTTP Status]: {response.status_code}")
    print("[Response Body]:")
    try:
        # Format JSON print if applicable
        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text)
except Exception as e:
    print(f"Network error occurred: {e}")
