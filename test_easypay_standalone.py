import requests
import json

def test_easypay_standalone():
    # --- CONFIGURATION ---
    client_id = "8cc8b631-044e-424a-9e7d-b02468e0f106"
    client_secret = "VKmlOh9YK!8j8#vwAPAJFthNctxDW4FPCxT0biOKJrSpZx2Jpjqs3y0loBp6NCd40ACBIxmRLlP3yr4X9o5HMxVSbxJFPlR#Pq5WYYPOeX2QIE33w0fsUQtUTYN8Yo03"
    
    # --- TEST DATA ---
    payload = {
        'trans_client_id': '255700000000', 
        'trans_amount': '100',
        'trans_reference': 'STANDALONE_TEST'
    }
    
    headers_alt = {
        'x-api-key': client_id,
        'User-Agent': 'Mozilla/5.0'
    }

    headers_orig = {
        'x-api-key': client_secret,
        'User-Agent': 'Mozilla/5.0'
    }

    # Trying variations including main domain
    variations = [
        "https://api.easypay.co.tz/v2/transaction/create",
        "https://easypay.co.tz/v2/transaction/create",
        "https://www.easypay.co.tz/v2/transaction/create",
        "https://api.easypay.co.tz/v2/transaction/create/",
    ]
    
    print("🚀 Starting Standalone EasyPay Test Variations...")
    
    # Add root check
    print("\n--- Testing Root URL: https://api.easypay.co.tz/ ---")
    try:
        r_root = requests.get("https://api.easypay.co.tz/", headers=headers, timeout=10)
        print(f"Root Status: {r_root.status_code}")
        print(f"Root Content (first 200 chars): {r_root.text[:200]}")
    except Exception as e:
        print(f"Root Error: {str(e)}")

    for url in variations:
        for name, h in [("SECRET_KEY", headers_orig), ("CLIENT_ID", headers_alt)]:
            print(f"\n--- Testing URL: {url} with {name} ---")
            try:
                response = requests.post(url, headers=h, data=payload, timeout=10, allow_redirects=False)
                print(f"POST Status: {response.status_code}")
                
                if response.status_code != 404:
                    print(f"✅ FOUND SOMETHING! Response: {response.text}")
                    break
            except Exception as e:
                print(f"❌ Error: {str(e)}")

if __name__ == "__main__":
    test_easypay_standalone()
