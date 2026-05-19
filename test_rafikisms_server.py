import json
import requests

url = "https://api.rafikisms.com/v1/vendor/send-sms"
payload = {
    "phone": "255616622485",
    "message": "OTP test from server",
    "sender_id": "Orbit"
}
headers = {
    "Content-Type": "application/json",
    "X-API-Key": "sk_qUvgWy4fU1btBrmEnQM2ggYr5IZieRMU0YvOrhWbdo5KVKxB"
}

print(f"Payload: {json.dumps(payload)}")
print(f"Headers: {headers}")

try:
    response = requests.post(url, json=payload, headers=headers, timeout=10)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")
    if response.status_code == 200:
        data = response.json()
        print(f"Success: {data.get('success')}")
        print(f"Message: {data.get('message')}")
        print(f"Data: {data.get('data')}")
    else:
        print(f"Error response: {response.text}")
except Exception as e:
    print(f"Error: {e}")