import hashlib
import json
import base64
from datetime import datetime
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
import os

class AirpayTransformer:
    """
    Utility class for Airpay Tanzania's Cryptography requirements:
    - AES-256-CBC Encryption (PKCS5Padding)
    - Checksum generation (SHA-256)
    """

    @staticmethod
    def encrypt(data_str: str, secret_key: str) -> str:
        """
        Encrypts data using AES-256-CBC.
        Implementation matches Airpay PHP sample:
        1. Generate 8 random bytes, hex encode to get 16-byte IV.
        2. Encrypt with PKCS5 padding.
        3. Prepend hex-IV to base64-encoded encrypted data.
        """
        # Generate random 8 bytes and hex encode to get 16 chars (16 bytes for IV)
        iv_hex = os.urandom(8).hex() # 16 chars
        iv_bytes = iv_hex.encode('utf-8')
        
        # Key must be 32 bytes for AES-256
        # According to docs, the key is md5(username . "~:~" . password)
        # Note: If secret_key is already the MD5, use it. 
        # But we'll ensure it's handled.
        key_bytes = secret_key.encode('utf-8')
        
        cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
        
        # Pad data (PKCS5 is equivalent to PKCS7 with block size 16)
        padded_data = pad(data_str.encode('utf-8'), AES.block_size)
        
        encrypted_bytes = cipher.encrypt(padded_data)
        
        # Airpay format: IV_HEX + Base64(Encrypted_Bytes)
        return iv_hex + base64.b64encode(encrypted_bytes).decode('utf-8')

    @staticmethod
    def decrypt(encrypted_str: str, secret_key: str) -> dict:
        """
        Decrypts data from Airpay response.
        1. Extract first 16 chars as IV.
        2. Decrypt the rest (Base64 decoded).
        """
        try:
            iv_hex = encrypted_str[:16]
            iv_bytes = iv_hex.encode('utf-8')
            
            encrypted_payload = encrypted_str[16:]
            encrypted_bytes = base64.b64decode(encrypted_payload)
            
            key_bytes = secret_key.encode('utf-8')
            
            cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
            
            decrypted_padded = cipher.decrypt(encrypted_bytes)
            decrypted_data = unpad(decrypted_padded, AES.block_size)
            
            return json.loads(decrypted_data.decode('utf-8'))
        except Exception as e:
            raise ValueError(f"Airpay Decryption Failed: {str(e)}")

    @staticmethod
    def calculate_checksum(data_dict: dict, date_str: str = None) -> str:
        """
        Generates checksum for Airpay Tanzania.
        Formula: SHA-256(Concatenated Values of Sorted Keys + YYYY-MM-DD)
        """
        if date_str is None:
            date_str = datetime.now().strftime('%Y-%m-%d')
            
        # Sort keys alphabetically
        sorted_keys = sorted(data_dict.keys())
        # Concatenate all values
        combined_values = "".join(str(data_dict[k]) for k in sorted_keys)
        
        checksum_base = combined_values + date_str
        return hashlib.sha256(checksum_base.encode('utf-8')).hexdigest()

    @staticmethod
    def verify_secure_hash(received_hash, data_dict, username):
        """
        Verifies Airpay ap_SecureHash using CRC32.
        Formula: crc32(TRANSACTIONID. : .APTRANSACTIONID. : .AMOUNT. : .TRANSACTIONSTATUS. : .MESSAGE. : .MID. : .USERNAME)
        Note: Separator is " : "
        """
        import zlib
        
        # Ensure values are strings and handle None
        def s(val): return str(val) if val is not None else ""
        
        # The fields must be in this specific order
        base_str = (
            s(data_dict.get('orderid')) + " : " +
            s(data_dict.get('ap_transactionid')) + " : " +
            s(data_dict.get('amount')) + " : " +
            s(data_dict.get('transaction_status')) + " : " +
            s(data_dict.get('message')) + " : " +
            s(data_dict.get('merchant_id')) + " : " +
            s(username)
        )
        
        # Calculate CRC32 and compare
        calculated_hash = zlib.crc32(base_str.encode('utf-8')) & 0xFFFFFFFF
        return str(received_hash) == str(calculated_hash)

    @staticmethod
    def generate_token(username, password, secret, transaction_id, api_key):
        """
        Special Token logic for Airpay Split Payout API:
        PRIVATE_KEY = hash('SHA256', APIKEY.'@'.USERNAME.’:|:’.PASSWORD);
        Token generation logic = hash_hmac('sha256',PRIVATE_KEY.TRANSACTION_ID,APIKEY);
        """
        import hmac
        
        # Step 1: Private Key
        pk_base = f"{api_key}@{username}:|:{password}"
        private_key = hashlib.sha256(pk_base.encode('utf-8')).hexdigest()
        
        # Step 2: Hmac SHA256
        msg = f"{private_key}{transaction_id}"
        token = hmac.new(api_key.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256).hexdigest()
        
        return token
