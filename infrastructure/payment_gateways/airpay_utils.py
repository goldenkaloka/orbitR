import hashlib
import json
import base64
import datetime
import logging
import zlib
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from django.conf import settings

logger = logging.getLogger(__name__)

class AirpayTransformer:
    """
    Official Airpay Python Integration Logic (v4)
    Ref: airpay_pythont_tz_v4/sendtoairpaypage.py and responsefromairpay.py
    """

    @staticmethod
    def encrypt_string(my_string, username, password):
        """
        Official encryption logic using AES-CBC with a hardcoded IV.
        """
        secret_key = hashlib.md5((username + "~:~" + password).encode()).hexdigest()
        
        # Official sample uses this hardcoded IV
        iv = 'c0f9e2d16031b0ce'  

        # Pad the request data and encrypt using AES-CBC with the generated key and IV
        cipher = AES.new(secret_key.encode(), AES.MODE_CBC, iv.encode())
        encrypted_data = cipher.encrypt(pad(my_string.encode(), AES.block_size))

        # Combine IV and encrypted data, then base64 encode
        data = iv + base64.b64encode(encrypted_data).decode()
        return data

    @staticmethod
    def decrypt_string(encrypted_data, username, password):
        """
        Official decryption logic.
        """
        secret_key = hashlib.md5((username + "~:~" + password).encode()).hexdigest()
        iv = encrypted_data[:16]
        encrypted_data = encrypted_data[16:]

        # Decrypt the data using AES-CBC with the given IV and secret key
        cipher = AES.new(secret_key.encode(), AES.MODE_CBC, iv.encode())
        decrypted_data = cipher.decrypt(base64.b64decode(encrypted_data))

        # Remove padding from the decrypted data
        unpadded_data = unpad(decrypted_data, AES.block_size)
        return unpadded_data.decode()

    @staticmethod
    def encrypt_sha(data, salt):
        """
        Official SHA-256 wrapper.
        Used for Private Key: sha256(secret + "@" + data)
        """
        key = hashlib.sha256((salt + '@' + data).encode()).hexdigest()
        return key

    @staticmethod
    def checksum_cal(post_data):
        """
        Official checksum calculation for initiation.
        SHA-256 of sorted values + date.
        """
        sorted_data = sorted(post_data.items(), key=lambda x: x[0])
        data_str = ''.join([str(value) for _, value in sorted_data])
        # Append current date in YYYY-MM-DD
        data_to_hash = data_str + datetime.datetime.now().strftime("%Y-%m-%d")
        return hashlib.sha256(data_to_hash.encode()).hexdigest()

    @staticmethod
    def verify_checksum_v4(data, mercid, username):
        """
        Official callback verification logic using CRC32.
        Ref: responsefromairpay.py:87
        """
        transaction_id = data.get('orderid', '').strip()
        ap_transaction_id = data.get('ap_transactionid', '').strip()
        amount = data.get('amount', '').strip()
        status = data.get('transaction_status', '').strip()
        message = data.get('message', '').strip()
        received_hash = str(data.get('ap_securehash', '')).strip()
        
        chmod = data.get('chmod', '').strip()
        customervpa = ""
        if chmod.lower() == "upi":
            vpa = data.get('CUSTOMERVPA', '').strip()
            if vpa:
                customervpa = ":" + vpa

        # Official string format for CRC32
        hash_string = f"{transaction_id}:{ap_transaction_id}:{amount}:{status}:{message}:{mercid}:{username}{customervpa}"
        
        # Calculating the CRC32 hash
        merchant_secure_hash = zlib.crc32(hash_string.encode())
        # Converting to unsigned integer
        merchant_secure_hash = merchant_secure_hash & 0xffffffff
        
        return str(merchant_secure_hash) == received_hash
