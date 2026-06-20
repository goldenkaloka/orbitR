import base64
import hashlib
import json

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

# Log from the webview
response_log = "e8a965314e655688Duf+hBxgeKBs+WK9PunPaE7+8yhZfRqOd86rj+wGTz1QcJ0Co1FRXvgJ4hfs9D/YCVPPGPewWJrP5DQe6Xl3PjxiQus4UHWEfET5+wK1wFiuSQmOLGh+IMPKmFmwRzLHI3geo05cnMGUobrgkEwEqiMoLMoa4FjzjcxLsjL8wQU="

try:
    print(decrypt_string(response_log, USERNAME, PASSWORD))
except Exception as e:
    print(f"Error: {e}")
