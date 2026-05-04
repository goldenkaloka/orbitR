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
response_log = "aed02e742ee5ac50H\/w3Zb\/TSe3uncOBVKdon5ytD92qZlMytZDr+\/V+20vZTX012UUqj+0OHtw8j6gx8OScMHdCqCGJnldbCGbeKzR8rjDRH9E9MqcJPDGj\/Bad6uR1R1i41\/u1n5ydF2j6LphgXUtajT\/J\/9BnHkXVJatIY0SU1juYFhgh6vlQLaI="
# Clean the backslashes
response_log = response_log.replace("\\/", "/")

try:
    print(decrypt_string(response_log, USERNAME, PASSWORD))
except Exception as e:
    print(f"Error: {e}")
