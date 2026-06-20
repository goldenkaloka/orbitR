import base64
import datetime
import hashlib
import json
import os
import re

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


# --- 🛰️ Manual .env Reader (Keeps it Standalone) ---
def get_env_var(var_name):
    # Try to find .env in current or parent directory
    env_paths = ['.env', '../.env']
    for path in env_paths:
        if os.path.exists(path):
            with open(path, 'r') as f:
                content = f.read()
                # Look for VAR=VALUE
                match = re.search(f'^{var_name}=(.*)$', content, re.M)
                if match:
                    return match.group(1).strip().strip('"').strip("'")
    return os.environ.get(var_name)

# --- 🔑 Credentials ---
CONFIG = {
    "AIRPAY_MERCHANT_ID": get_env_var("AIRPAY_MERCHANT_ID") or "247234",
    "AIRPAY_CLIENT_ID": get_env_var("AIRPAY_CLIENT_ID"),
    "AIRPAY_CLIENT_SECRET": get_env_var("AIRPAY_CLIENT_SECRET"),
    "AIRPAY_USERNAME": get_env_var("AIRPAY_USERNAME"),
    "AIRPAY_PASSWORD": get_env_var("AIRPAY_PASSWORD"),
    "AIRPAY_SECRET_KEY": get_env_var("AIRPAY_SECRET_KEY") or get_env_var("AIRPAY_SECRET"),
}

class AirpayStandaloneTest:
    def __init__(self):
        self.mercid = CONFIG["AIRPAY_MERCHANT_ID"]
        self.client_id = CONFIG["AIRPAY_CLIENT_ID"]
        self.client_secret = CONFIG["AIRPAY_CLIENT_SECRET"]
        self.username = CONFIG["AIRPAY_USERNAME"]
        self.password = CONFIG["AIRPAY_PASSWORD"]
        self.secret = CONFIG["AIRPAY_SECRET_KEY"]
        
        self.oauth_url = "https://kraken.airpay.tz/airpay/pay/v1/api/oauth2/"
        self.seamless_url = "https://kraken.airpay.tz/airpay/pay/v1/api/seamless/index.php"

        # Validate we have what we need
        missing = [k for k, v in CONFIG.items() if not v]
        if missing:
            print(f"!! WARNING: Missing variables in .env: {', '.join(missing)} !!")

    def encrypt_data(self, data_dict):
        """Step 2: Generate encryption key and encrypt JSON"""
        # key = md5(username + "~:~" + password)
        key_raw = f"{self.username}~:~{self.password}"
        secret_key = hashlib.md5(key_raw.encode()).hexdigest()
        
        iv = 'c0f9e2d16031b0ce' # Hardcoded IV as per v4 SDK
        my_string = json.dumps(data_dict)
        
        cipher = AES.new(secret_key.encode(), AES.MODE_CBC, iv.encode())
        encrypted_bytes = cipher.encrypt(pad(my_string.encode(), AES.block_size))
        return iv + base64.b64encode(encrypted_bytes).decode()

    def decrypt_data(self, encrypted_str):
        """Decrypt helper for responses"""
        key_raw = f"{self.username}~:~{self.password}"
        secret_key = hashlib.md5(key_raw.encode()).hexdigest()
        
        iv = encrypted_str[:16]
        data = encrypted_str[16:]
        
        cipher = AES.new(secret_key.encode(), AES.MODE_CBC, iv.encode())
        decrypted_bytes = cipher.decrypt(base64.b64decode(data))
        return unpad(decrypted_bytes, AES.block_size).decode()

    def run(self):
        print("\n--- 🕵️ AIRPAY PRODUCTION DIAGNOSTIC (AUTO .ENV) ---\n")
        print(f"Merchant ID: {self.mercid}")
        print(f"Username: {self.username}")
        
        # --- PHASE 1: OAUTH ---
        print("\n[1/2] ATTEMPTING AUTHENTICATION...")
        current_date = datetime.datetime.now().strftime("%Y-%m-%d")
        
        auth_payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "merchant_id": self.mercid,
            "mercid": self.mercid
        }
        
        enc_auth = self.encrypt_data(auth_payload)
        
        # Checksum calculation: values sorted by keys + current_date
        sorted_keys = sorted(auth_payload.keys())
        data_str = "".join([str(auth_payload[k]) for k in sorted_keys])
        checksum_auth = hashlib.sha256((data_str + current_date).encode()).hexdigest()

        print(f"POST {self.oauth_url}")
        
        try:
            resp_auth = requests.post(self.oauth_url, data={
                'merchant_id': self.mercid,
                'encdata': enc_auth,
                'checksum': checksum_auth
            }, timeout=15)
            
            print(f"HTTP Status: {resp_auth.status_code}")
            
            if resp_auth.status_code != 200:
                print(f"RAW ERROR BODY: {resp_auth.text}")
                return

            res_json = resp_auth.json()
            if 'response' in res_json:
                decrypted_auth = self.decrypt_data(res_json['response'])
                print(f"Decrypted Response: {decrypted_auth}")
                
                token_data = json.loads(decrypted_auth)
                token = token_data.get('data', {}).get('access_token')
                
                if not token:
                    print("!! Access Token missing from decrypted response !!")
                    return
            else:
                print(f"!! Response JSON was unexpected: {res_json} !!")
                return
                
        except Exception as e:
            print(f"!! OAUTH CRASH: {str(e)} !!")
            return

        # --- PHASE 2: SEAMLESS ---
        print("\n[2/2] ATTEMPTING SEAMLESS USSD PUSH...")
        
        seamless_payload = {
            'buyer_email': 'test@mwanachuoshop.com',
            'buyer_phone': '255616622485',
            'buyer_firstname': 'Mwanachuo',
            'buyer_lastname': 'Test',
            'orderid': 'DIAG' + datetime.datetime.now().strftime("%H%M%S"),
            'amount': '100.00',
            'channel': 'mmoney',
            'chmod': 'mmoney',
            'bankcode': 'MPESA',
            'currency_code': '834',
            'iso_currency': 'TZS',
            'merchant_id': self.mercid
        }
        
        enc_seamless = self.encrypt_data(seamless_payload)
        
        # PrivateKey: sha256(secret@username:|:password)
        private_key_raw = f"{self.secret}@{self.username}:|:{self.password}"
        private_key = hashlib.sha256(private_key_raw.encode()).hexdigest()
        
        # Checksum
        sorted_keys_s = sorted(seamless_payload.keys())
        data_str_s = "".join([str(seamless_payload[k]) for k in sorted_keys_s])
        checksum_s = hashlib.sha256((data_str_s + current_date).encode()).hexdigest()
        
        print(f"POST {self.seamless_url}?token=HIDDEN")
        
        try:
            resp_s = requests.post(f"{self.seamless_url}?token={token}", data={
                'merchant_id': self.mercid,
                'privatekey': private_key,
                'encdata': enc_seamless,
                'checksum': checksum_s
            }, timeout=15)
            
            print(f"HTTP Status: {resp_s.status_code}")
            
            # --- ROBUST OUTER PARSING ---
            raw_text = resp_s.text.strip()
            print(f"RAW SERVER RESPONSE: {raw_text}")
            
            try:
                res_body = json.loads(raw_text)
            except json.JSONDecodeError:
                print("!! OUTER JSON PARSE FAILED - Cleaning trailing junk !!")
                last_brace = raw_text.rfind('}')
                res_body = json.loads(raw_text[:last_brace+1])

            if 'response' in res_body:
                decrypted_s = self.decrypt_data(res_body['response'])
                print(f"RAW DECRYPTED RESPONSE: {decrypted_s}")
                
                try:
                    res_data = json.loads(decrypted_s)
                    print(f"RECOVERED DATA: {json.dumps(res_data, indent=2)}")
                except Exception:
                    # Final inner cleaning
                    print("!! INNER JSON PARSE FAILED - Cleaning inner junk !!")
                    last_brace_inner = decrypted_s.rfind('}')
                    res_data = json.loads(decrypted_s[:last_brace_inner+1])
                    print(f"RECOVERED DATA (CLEANED): {json.dumps(res_data, indent=2)}")
            else:
                print(f"Unexpected JSON format: {res_body}")
            
        except Exception as e:
            print(f"!! SEAMLESS CRASH: {str(e)} !!")

        print("\n--- DIAGNOSTIC COMPLETE ---")

if __name__ == "__main__":
    # Dependency check
    try:
        from Crypto.Cipher import AES
    except ImportError:
        print("Please run: pip install pycryptodome requests")
    else:
        test = AirpayStandaloneTest()
        test.run()
