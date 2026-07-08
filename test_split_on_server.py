import sys
import os
import hashlib
import json
import datetime
import requests
import hmac

def format_phone(phone):
    p = str(phone).replace('+', '').replace(' ', '')
    if p.startswith('0'):
        p = '255' + p[1:]
    return p

# Load .env manually from current folder or parent folders
env = {}
env_paths = ['.env', '../.env', '../../.env', '/app/.env']
loaded = False
for path in env_paths:
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if line.strip() and not line.startswith('#') and '=' in line:
                    k, v = line.strip().split('=', 1)
                    env[k.strip()] = v.strip()
        loaded = True
        print(f"Loaded credentials from {path}")
        break

if not loaded:
    print("Error: Could not find .env file.")
    sys.exit(1)

mercid = env.get('AIRPAY_MERCHANT_ID')
username = env.get('AIRPAY_USERNAME')
password = env.get('AIRPAY_PASSWORD')
secret_key = env.get('AIRPAY_SECRET_KEY')  # APIKEY
primary_account = env.get('AIRPAY_PRIMARY_ACCOUNT') or mercid

if not all([mercid, username, password, secret_key]):
    print("Error: Missing required Airpay configuration in .env.")
    sys.exit(1)

print("\n--- Airpay Split Settlement Test ---")
print("Merchant ID:", mercid)
print("Primary Account (Platform):", primary_account)

# Get inputs from command line arguments or interactive prompts
if len(sys.argv) >= 5:
    transaction_id = sys.argv[1]
    rrn = sys.argv[2]
    recipient_phone = sys.argv[3]
    split_amount = sys.argv[4]
else:
    transaction_id = input("Enter Airpay Transaction ID (from a successful payment): ").strip()
    rrn = input("Enter Transaction RRN: ").strip()
    recipient_phone = input("Enter Recipient Phone Number (e.g. 0712345678): ").strip()
    split_amount = input("Enter Split Amount (in TZS, e.g. 1000): ").strip()

if not all([transaction_id, rrn, recipient_phone, split_amount]):
    print("Error: All inputs are required.")
    sys.exit(1)

formatted_phone = format_phone(recipient_phone)
amount_int = int(float(split_amount))

# Token generation
# PRIVATE_KEY = hash('SHA256', APIKEY.'@'.USERNAME.':|:'.PASSWORD)
raw_priv = f"{secret_key}@{username}:|:{password}"
private_key = hashlib.sha256(raw_priv.encode()).hexdigest()

# Token = hash_hmac('sha256', PRIVATE_KEY.TRANSACTION_ID, APIKEY)
token = hmac.new(secret_key.encode(), f"{private_key}{transaction_id}".encode(), hashlib.sha256).hexdigest()

headers = {
    "Content-Type": "application/json",
    "Api-Key": secret_key
}

# The sum of other split values and primary split value must equal the transaction amount.
# Let's assume we are testing splitting a single recipient's share from the transaction.
# We need to specify the primary account's split value as the remainder.
# We will ask for the total transaction amount to calculate the primary account's share correctly.
total_amount_str = input("Enter Total Transaction Paid Amount (e.g. 2800): ").strip()
total_amount = int(float(total_amount_str))

priacct_split_value = total_amount - amount_int
if priacct_split_value < 0:
    print(f"Error: Split amount ({amount_int}) cannot exceed total transaction amount ({total_amount}).")
    sys.exit(1)

# Generate a unique SPLIT_REQUEST_ID using timestamp
import time
split_request_id = str(int(time.time()))[-15:]

payload = {
    "TRANSACTION_ID": transaction_id,
    "RRN": rrn,
    "SPLIT_REQUEST_ID": split_request_id,
    "PRIACCT_IDENTIFIER": primary_account,
    "PRIACCT_SPLIT_VALUE": priacct_split_value,
    "OTHER_IDENTIFIERS": formatted_phone,
    "SPLIT_VALUES": amount_int,
    "TOKEN": token
}

print("\nPayload:")
print(json.dumps(payload, indent=2))

url = "https://kraken.airpay.tz/airpay/neft/split_payout/transaction_level_split_config_api.php"

try:
    response = requests.post(url, headers=headers, json=payload, timeout=15)
    print("\n--- Response ---")
    print("Status Code:", response.status_code)
    print("Response Body:", response.text)
except Exception as e:
    print("HTTP Request Error:", e)
