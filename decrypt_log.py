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

# Log from the latest failure
response_log = "f38c9413c19d94ebwb5gVmJ/Mr1/YpuDtUyXz+ENm8dyzt6n/Cjk6kB3uyI/7hODLSFeMKl7C71cuOQZDIQruPHGoNLr7Gb++yrf+4/khNVRusBwZfxLpPJ2Sxs5fkGRzJK/mD+DBWZ0pu3PRyYDLUskCN2105JFRFqGt7WqxCTQPgMLQKRGNd0UHrS2t37P8bq2PQ+6zKGXjQfVTtClRgQE4Ggs5nmeqcFvQ1monIlFj7/ojIr9EC1AGJjyi02bazRO3AquBU33MMoeOVOzuMLCVRO1B2yMUS3XpA=="

try:
    print(decrypt_string(response_log, USERNAME, PASSWORD))
except Exception as e:
    print(f"Error: {e}")
