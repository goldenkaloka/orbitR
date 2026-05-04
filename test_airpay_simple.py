import hashlib
import base64
import zlib
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

# --- CONFIGURATION (Match your .env) ---
MERCHANT_ID = "247234"
USERNAME = "9416308"
PASSWORD = "VPz2eXjJ"
SECRET_KEY = "DXfBQn277z2Emtjt"

# --- CORE LOGIC (Extracted from Transformer) ---
def encrypt_string(my_string, username, password):
    secret_key = hashlib.md5((username + "~:~" + password).encode()).hexdigest()
    iv = 'c0f9e2d16031b0ce'  
    cipher = AES.new(secret_key.encode(), AES.MODE_CBC, iv.encode())
    encrypted_data = cipher.encrypt(pad(my_string.encode(), AES.block_size))
    return iv + base64.b64encode(encrypted_data).decode()

def encrypt_sha(data, salt):
    return hashlib.sha256((salt + '@' + data).encode()).hexdigest()

# --- THE TEST ---
def run_test():
    print("🚀 STARTING AIRPAY LOGIC TEST (STANDALONE)")
    print("-" * 40)
    
    # 1. Test Initiation Params (What Flutter needs)
    order_id = "TEST-ORDER-001"
    amount = "1500.00"
    
    # Flutter SDK needs the SHA256(secret + "@" + user:|:pass)
    private_key = encrypt_sha(USERNAME + ":|:" + PASSWORD, SECRET_KEY)
    
    print(f"Order ID: {order_id}")
    print(f"Amount: {amount}")
    print(f"Calculated Private Key: {private_key}")
    print("-" * 40)

    # 2. Test Webhook Verification (Checksum)
    ap_transaction_id = "AP123456789"
    status = "200"
    message = "Success"
    
    # format: orderid:ap_transactionid:amount:status:message:mercid:username
    hash_string = f"{order_id}:{ap_transaction_id}:{amount.split('.')[0]}:{status}:{message}:{MERCHANT_ID}:{USERNAME}"
    secure_hash = zlib.crc32(hash_string.encode()) & 0xffffffff
    
    print(f"Simulating Callback for {ap_transaction_id}")
    print(f"Hash String: {hash_string}")
    print(f"Generated CRC32 Hash: {secure_hash}")
    print("-" * 40)

    # 3. Test Encryption (Simulating a response payload)
    test_data = '{"data": {"status": "success", "orderid": "TEST-001"}}'
    encrypted = encrypt_string(test_data, USERNAME, PASSWORD)
    print(f"Encrypted Sample Payload: {encrypted[:40]}...")
    
    print("\n✅ LOGIC TEST COMPLETE!")
    print("If these values match what Airpay expects, your integration is correct.")

if __name__ == "__main__":
    try:
        run_test()
    except Exception as e:
        print(f"❌ TEST FAILED: {str(e)}")
        print("\nNote: Make sure 'pycryptodome' is installed: pip install pycryptodome")
