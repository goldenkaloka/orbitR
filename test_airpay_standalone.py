import base64
import hashlib
import json
import os
from datetime import datetime

import requests

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
except ImportError:
    print("🔥 Missing pycryptodome library. Run: pip install pycryptodome")
    exit(1)

def calculate_checksum(data):
    """
    1. Collect all key-value pairs.
    2. Arrange alphabetically by keys.
    3. Combine values into a string.
    4. Append current date YYYY-MM-DD.
    5. SHA-256 hash.
    """
    sorted_data = dict(sorted(data.items()))
    checksumdata = ""
    for key, value in sorted_data.items():
        checksumdata += str(value)
    
    current_date = datetime.now().strftime('%Y-%m-%d')
    checksumdata += current_date
    
    return hashlib.sha256(checksumdata.encode('utf-8')).hexdigest()

def encrypt_data(data_str, key_str):
    """
    AES-256-CBC with PKCS5Padding.
    IV is 16 chars (8 bytes hex encoded).
    Returns IV + Base64(Encrypted Data).
    """
    iv_bytes = os.urandom(8)
    iv_hex = iv_bytes.hex()  # 16 characters
    iv = iv_hex.encode('utf-8')
    
    key = key_str.encode('utf-8')
    if len(key) > 32:
        key = key[:32]
    elif len(key) < 32:
        key = key.ljust(32, b'\0')
        
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct_bytes = cipher.encrypt(pad(data_str.encode('utf-8'), AES.block_size))
    
    return iv_hex + base64.b64encode(ct_bytes).decode('utf-8')

def decrypt_data(response_str, key_str):
    """
    Decrypts the AES/CBC/PKCS5PADDING data.
    """
    iv_hex = response_str[:16]
    encrypted_b64 = response_str[16:]
    
    iv = iv_hex.encode('utf-8')
    key = key_str.encode('utf-8')
    
    if len(key) > 32:
        key = key[:32]
    elif len(key) < 32:
        key = key.ljust(32, b'\0')
        
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct_bytes = base64.b64decode(encrypted_b64)
    
    pt_bytes = unpad(cipher.decrypt(ct_bytes), AES.block_size)
    return pt_bytes.decode('utf-8')

def test_airpay_standalone():
    print("🚀 Initializing Standalone Airpay Test...")
    
    # Credentials from your .env
    merchant_id   = "247234"
    client_id     = "d0e53f"
    client_secret = "2da4b10b2a93a4fb99144f821bc38aa4"
    secret_key    = "2da4b10b2a93a4fb99144f821bc38aa4"
    
    data = {
        'client_id': client_id,
        'client_secret': client_secret,
        'merchant_id': merchant_id,
        'grant_type': 'client_credentials'
    }
    
    # 1. Encrypt Data
    data_json = json.dumps(data)
    encdata = encrypt_data(data_json, secret_key)
    
    # 2. Calculate Checksum
    chksum = calculate_checksum(data)
    
    # 3. Prepare Payload
    payload = {
        'merchant_id': merchant_id,
        'encdata': encdata,
        'checksum': chksum
    }
    
    url = "https://kraken.airpay.tz/airpay/pay/v1/api/oauth2/"
    
    print(f"\nAttempting Authentication with Airpay...")
    print(f"URL: {url}")
    print(f"Checksum: {chksum}")
    print("-" * 50)
    
    try:
        # Send Request
        response = requests.post(url, data=payload, timeout=15)
        
        print(f"\nStatus Code: {response.status_code}")
        print(f"Raw Response: {response.text}")
        
        if response.status_code == 200:
            resp_json = response.json()
            if resp_json.get('status') == 'success':
                enc_response = resp_json.get('response')
                if enc_response:
                    decrypted_data = decrypt_data(enc_response, secret_key)
                    print("\n✅ SUCCESS! Authentication Passed.")
                    print(f"Decrypted Access Token Data: \n{json.dumps(json.loads(decrypted_data), indent=2)}")
                else:
                    print("\n✅ SUCCESS! But no encrypted payload was returned.")
            else:
                print("\n❌ FAILED. Airpay rejected the credentials.")
        else:
            print("\n❌ FAILED. Check network or endpoint URL.")
            
    except Exception as e:
        print(f"\n🔥 EXCEPTION OCCURRED: {str(e)}")

if __name__ == "__main__":
    test_airpay_standalone()
