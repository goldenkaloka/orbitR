import requests

# =============================
# EASY PAY CONFIG
# =============================
API_URL = "https://api.easypay.co.tz/v2/transaction/create"
API_KEY = "VKmlOh9YK!8j8#vwAPAJFthNctxDW4FPCxT0biOKJrSpZx2Jpjqs3y0loBp6NCd40ACBIxmRLlP3yr4X9o5HMxVSbxJFPlR#Pq5WYYPOeX2QIE33w0fsUQtUTYN8Yo03"

# =============================
# TEST DATA
# =============================
payload = {
    "trans_client_id": "255700000000",   # customer phone
    "trans_amount": "100",
    "trans_reference": "OrbitR Test"
}

headers = {
    "x-api-key": API_KEY
}


def push_payment():
    try:
        print(f"🚀 Sending request to {API_URL}...")
        response = requests.post(
            API_URL,
            headers=headers,
            data=payload,
            timeout=30
        )

        print("STATUS CODE:", response.status_code)

        # Try JSON first
        try:
            print("RESPONSE JSON:")
            print(response.json())
        except Exception:
            print("RAW RESPONSE:")
            print(response.text)

    except requests.exceptions.RequestException as e:
        print("Request failed:", e)


if __name__ == "__main__":
    push_payment()
