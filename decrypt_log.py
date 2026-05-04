import hashlib
import json
import base64
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

USERNAME = "9416308"
PASSWORD = "VPz2eXjJ"

def decrypt_string(encrypted_data, username, password):
    secret_key = hashlib.md5((username + "~:~" + password).encode()).hexdigest()
    iv = encrypted_data[:16]
    encrypted_data = encrypted_data[16:]
    cipher = AES.new(secret_key.encode(), AES.MODE_CBC, iv.encode())
    decrypted_data = cipher.decrypt(base64.b64decode(encrypted_data))
    return unpad(decrypted_data, AES.block_size).decode()

# Log from the user request
response_log = "892152d66f5394eak3Bqr9xIqw+IWAavt2b9/2QGiCfxVB8Di/euHZBSRwyAYUrVtnsm7FCOcjzlEII5T4mnnPnQY4vb9zPNUuWRKIB7KJb03phXCs/vt77UasG4GFYw0e4+luRMq7e+gX4IcmWGTLSIIIRdtLeTAx/P94h/UwVNegYDagWd4ww2yzg="

try:
    print(decrypt_string(response_log, USERNAME, PASSWORD))
except Exception as e:
    print(f"Error: {e}")
