import hashlib
import json
import base64
import requests
import datetime
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# Load credentials from .env manually for standalone test
def get_env_var(name):
    with open('.env', 'r') as f:
        for line in f:
            if line.startswith(f"{name}="):
                return line.split('=')[1].strip()
    return None

MERCHANT_ID = get_env_var('AIRPAY_MERCHANT_ID')
USERNAME = get_env_var('AIRPAY_USERNAME')
PASSWORD = get_env_var('AIRPAY_PASSWORD')
SECRET_KEY = get_env_var('AIRPAY_SECRET_KEY')
CLIENT_ID = get_env_var('AIRPAY_CLIENT_ID')
CLIENT_SECRET = get_env_var('AIRPAY_CLIENT_SECRET')

OAUTH_URL = "https://kraken.airpay.tz/airpay/pay/v1/api/oauth2/"

def encrypt_string(my_string, username, password):
    secret_key = hashlib.md5((username + "~:~" + password).encode()).hexdigest()
    iv = 'c0f9e2d16031b0ce'  
    cipher = AES.new(secret_key.encode(), AES.MODE_CBC, iv.encode())
    encrypted_data = cipher.encrypt(pad(my_string.encode(), AES.block_size))
    data = iv + base64.b64encode(encrypted_data).decode()
    return data

def decrypt_string(encrypted_data, username, password):
    from Crypto.Util.Padding import unpad
    secret_key = hashlib.md5((username + "~:~" + password).encode()).hexdigest()
    iv = encrypted_data[:16]
    encrypted_data = encrypted_data[16:]
    cipher = AES.new(secret_key.encode(), AES.MODE_CBC, iv.encode())
    decrypted_data = cipher.decrypt(base64.b64decode(encrypted_data))
    return unpad(decrypted_data, AES.block_size).decode()

def test_token_request():
    print(f"🚀 TESTING AIRPAY TOKEN REQUEST")
    print(f"URL: {OAUTH_URL}")
    print(f"Merchant ID: {MERCHANT_ID}")
    
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
        "merchant_id": int(MERCHANT_ID)
    }
    
    # Calculate Checksum (Alphabetical: client_id, client_secret, grant_type, merchant_id)
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")
    all_data = f"{CLIENT_ID}{CLIENT_SECRET}client_credentials{MERCHANT_ID}{current_date}"
    checksum = hashlib.sha256(all_data.encode()).hexdigest()
    
    # Encrypt Payload
    enc_data = encrypt_string(json.dumps(payload), USERNAME, PASSWORD)
    
    post_data = {
        'merchant_id': MERCHANT_ID,
        'checksum': checksum,
        'encdata': enc_data
    }
    
    try:
        print("Sending request...")
        response = requests.post(OAUTH_URL, data=post_data, timeout=15)
        
        print(f"HTTP Status: {response.status_code}")
        print("Response Body:")
        print(response.text)
        
        if response.status_code == 200:
            res_json = response.json()
            if 'response' in res_json:
                decrypted = decrypt_string(res_json['response'], USERNAME, PASSWORD)
                print(f"\nDecrypted Response: {decrypted}")
                res_data = json.loads(decrypted)
                
                if res_data.get('status') == 'success' or res_data.get('data', {}).get('status') == 'success':
                    token = res_data.get('data', {}).get('access_token')
                    if token:
                        print(f"\n✅ SUCCESS! Token obtained: {token[:10]}...")
                    else:
                        print(f"\n❌ FAILED: Token missing in success response")
                else:
                    error_msg = res_data.get('data', {}).get('message') or res_data.get('message')
                    print(f"\n❌ FAILED: {error_msg}")
            else:
                print("\n❌ Error: Response field missing in JSON")
        else:
            print(f"\n❌ SERVER ERROR: {response.status_code}")
            
    except Exception as e:
        print(f"\n❌ ERROR: {str(e)}")

if __name__ == "__main__":
    test_token_request()
