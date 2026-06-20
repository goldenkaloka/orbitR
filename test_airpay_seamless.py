import base64
import datetime
import hashlib
import json
import time

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


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

def encrypt_string(my_string, username, password):
    secret_key = hashlib.md5((username + "~:~" + password).encode()).hexdigest()
    iv = 'c0f9e2d16031b0ce'
    cipher = AES.new(secret_key.encode(), AES.MODE_CBC, iv.encode())
    encrypted_data = cipher.encrypt(pad(my_string.encode(), AES.block_size))
    return iv + base64.b64encode(encrypted_data).decode()

def decrypt_string(encrypted_data, username, password):
    from Crypto.Util.Padding import unpad
    secret_key = hashlib.md5((username + "~:~" + password).encode()).hexdigest()
    iv = encrypted_data[:16]
    encrypted_data = encrypted_data[16:]
    cipher = AES.new(secret_key.encode(), AES.MODE_CBC, iv.encode())
    decrypted_data = cipher.decrypt(base64.b64decode(encrypted_data))
    return unpad(decrypted_data, AES.block_size).decode()

def checksum_cal(data):
    sorted_keys = sorted(data.keys())
    checksum_str = "".join([str(data[k]) for k in sorted_keys])
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")
    return hashlib.sha256((checksum_str + current_date).encode()).hexdigest()

def get_token():
    payload = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "client_credentials",
        "merchant_id": int(MERCHANT_ID)
    }
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")
    all_data = f"{CLIENT_ID}{CLIENT_SECRET}client_credentials{MERCHANT_ID}{current_date}"
    checksum = hashlib.sha256(all_data.encode()).hexdigest()
    enc_data = encrypt_string(json.dumps(payload), USERNAME, PASSWORD)

    response = requests.post("https://kraken.airpay.tz/airpay/pay/v1/api/oauth2/", data={
        'merchant_id': MERCHANT_ID, 'checksum': checksum, 'encdata': enc_data
    })
    decrypted = decrypt_string(response.json()['response'], USERNAME, PASSWORD)
    return json.loads(decrypted)['data']['access_token']

def test_seamless():
    print("Obtaining token...")
    token = get_token()
    print(f"Token: {token}")

    # TIGO and AIRTEL are configured on this merchant account
    # Replace phone numbers with real numbers to receive the USSD push
    banks = [
        ('TIGO',   '0654000000', '5000.00'),   # Replace with real Tigo number e.g. 0671XXXXXX
        ('AIRTEL', '0684000000', '5000.00'),   # Replace with real Airtel number e.g. 0685XXXXXX
    ]

    seamless_url = "https://kraken.airpay.tz/airpay/pay/v1/api/seamless/index.php"

    for bankcode, phone, amount in banks:
        # Use millisecond timestamp to guarantee a unique order ID every run
        order_id = str(int(time.time() * 1000))[-15:]
        print(f"\n--- Testing {bankcode} | OrderID: {order_id} ---")

        payload = {
            'buyer_email': 'test@mwanachuoshop.com',
            'buyer_phone': phone,
            'buyer_firstname': 'Test',
            'buyer_lastname': 'User',
            'orderid': order_id,
            'amount': amount,
            'channel': 'mmoney',
            'chmod': 'mmoney',
            'bankcode': bankcode,
            'currency_code': '834',
            'iso_currency': 'TZS',
            'merchant_id': MERCHANT_ID,
            'mer_dom': base64.b64encode("https://mwanachuoshop.com".encode()).decode()
        }

        private_key = hashlib.sha256((SECRET_KEY + '@' + USERNAME + ':|:' + PASSWORD).encode()).hexdigest()
        enc_data = encrypt_string(json.dumps(payload), USERNAME, PASSWORD)
        checksum = checksum_cal(payload)

        post_data = {
            'merchant_id': MERCHANT_ID,
            'privatekey': private_key,
            'encdata': enc_data,
            'checksum': checksum
        }

        response = requests.post(f"{seamless_url}?token={token}", data=post_data)
        print(f"  HTTP {response.status_code} | Raw: {response.text[:300]}")

        try:
            text = response.text.strip()
            if not text:
                print("  Empty response body")
                continue
            json_strings = text.replace("}{", "}|||{").split("|||") if "}{" in text else [text]
            for j_str in json_strings:
                if not j_str.strip():
                    continue
                res_json = json.loads(j_str)
                if 'response' in res_json:
                    decrypted = decrypt_string(res_json['response'], USERNAME, PASSWORD)
                    result = json.loads(decrypted)
                    code = result.get('status_code')
                    msg = result.get('message')
                    print(f"  Result: [{code}] {msg}")
                    if code == '200':
                        print(f"  SUCCESS - {bankcode} push sent!")
                        print(f"  Full: {decrypted}")
        except Exception as e:
            print(f"  Error: {e}")

        # Small delay between requests to avoid duplicate order ID collision
        time.sleep(1)

if __name__ == "__main__":
    test_seamless()
