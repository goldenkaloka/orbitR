import json
import requests

url = "https://api.rafikisms.com/v1/vendor/send-sms"

test_cases = [
    {
        "name": "with sender_id + sender",
        "payload": {"phone": "255616622485", "message": "OTP test", "sender_id": "Orbit", "sender": "Orbit"}
    },
    {
        "name": "with sender_id only",
        "payload": {"phone": "255616622485", "message": "OTP test", "sender_id": "Orbit"}
    },
    {
        "name": "no sender (default)",
        "payload": {"phone": "255616622485", "message": "OTP test"}
    },
    {
        "name": "local format 0616622485",
        "payload": {"phone": "0616622485", "message": "OTP test local format", "sender_id": "Orbit"}
    },
]

headers = {
    "Content-Type": "application/json",
    "X-API-Key": "sk_qUvgWy4fU1btBrmEnQM2ggYr5IZieRMU0YvOrhWbdo5KVKxB"
}

for tc in test_cases:
    print(f"\n=== Testing: {tc['name']} ===")
    try:
        response = requests.post(url, json=tc["payload"], headers=headers, timeout=10)
        print(f"Status: {response.status_code} | Response: {response.text}")
    except Exception as e:
        print(f"Error: {e}")