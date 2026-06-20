import base64
import hashlib
import hmac
import json
import os
from datetime import date
from typing import Any, Dict, List, Optional

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad


class AirpayClient:
    def __init__(
        self,
        merchant_id: str,
        username: str,
        password: str,
        secret: str,
        secret_key: str,
        api_key: Optional[str] = None,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        base_url_kraken: str = "https://kraken.airpay.tz",
        base_url_payments: str = "https://payments.airpay.tz",
    ) -> None:
        self.merchant_id = merchant_id
        self.username = username
        self.password = password
        self.secret = secret
        self.secret_key = secret_key
        self.api_key = api_key
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url_kraken = base_url_kraken
        self.base_url_payments = base_url_payments

        self._private_key = self._compute_private_key()

    def _compute_private_key(self) -> str:
        data = f"{self.secret}@{self.username}:|:{self.password}"
        return hashlib.sha256(data.encode()).hexdigest()

    @staticmethod
    def _format_date() -> str:
        return date.today().isoformat()

    def _checksum(self, data: Dict[str, Any]) -> str:
        sorted_keys = sorted(data.keys())
        concat_values = "".join(str(data[k]) for k in sorted_keys)
        concat_values += self._format_date()
        return hashlib.sha256(concat_values.encode()).hexdigest()

    def _encrypt(self, plain_json: str) -> str:
        iv_bytes = os.urandom(8)
        iv_hex = iv_bytes.hex()

        key_bytes = bytes.fromhex(self.secret_key)
        if len(key_bytes) < 32:
            key_bytes = key_bytes.ljust(32, b'\x00')

        cipher = AES.new(key_bytes, AES.MODE_CBC, iv=iv_hex.encode('ascii'))
        ciphertext = cipher.encrypt(pad(plain_json.encode('utf-8'), AES.block_size))

        return iv_hex + base64.b64encode(ciphertext).decode('ascii')

    def _decrypt(self, encrypted_text: str) -> Dict[str, Any]:
        iv_hex = encrypted_text[:16]
        b64_data = encrypted_text[16:]

        iv_ascii = iv_hex.encode('ascii')
        ciphertext = base64.b64decode(b64_data)

        key_bytes = bytes.fromhex(self.secret_key)
        if len(key_bytes) < 32:
            key_bytes = key_bytes.ljust(32, b'\x00')

        cipher = AES.new(key_bytes, AES.MODE_CBC, iv=iv_ascii)
        plain = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return json.loads(plain.decode('utf-8'))

    def get_access_token(self) -> Dict[str, Any]:
        if not self.client_id or not self.client_secret:
            raise ValueError("client_id and client_secret are required for OAuth2")

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "merchant_id": self.merchant_id,
            "grant_type": "client_credentials",
        }
        enc_data = self._encrypt(json.dumps(data))
        checksum = self._checksum(data)

        payload = {
            "merchant_id": self.merchant_id,
            "encdata": enc_data,
            "checksum": checksum,
        }

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        resp = requests.post(url, data=payload, headers=headers)
        
        # Avoid crashing immediately to print response text
        if resp.status_code != 200:
            print(f"Error {resp.status_code}: {resp.text}")
            
        resp.raise_for_status()
        resp_data = resp.json()

        if 'response' in resp_data:
            decrypted = self._decrypt(resp_data['response'])
            token_data = decrypted.get('data') or decrypted
            return {
                "access_token": token_data.get("access_token"),
                "expires_in": token_data.get("expires_in"),
                "scope": token_data.get("scope"),
            }
        return resp_data


if __name__ == "__main__":
    print("🚀 Testing AirpayClient Authentication...")
    
    # Credentials from your .env
    client = AirpayClient(
        merchant_id='247234',
        username='d0e53f',
        password='Genuine20',
        secret='2da4b10b2a93a4fb99144f821bc38aa4',
        secret_key='2da4b10b2a93a4fb99144f821bc38aa4',
        client_id='d0e53f',
        client_secret='2da4b10b2a93a4fb99144f821bc38aa4'
    )

    try:
        token_info = client.get_access_token()
        access_token = token_info.get("access_token")
        print("\n✅ SUCCESS! Authentication Passed.")
        print(f"Access Token: {access_token[:15]}...[truncated]")
        print(f"Expires In: {token_info.get('expires_in')} seconds")
    except Exception as e:
        print("\n❌ FAILED. Airpay rejected the request.")
        print(f"Error: {str(e)}")
