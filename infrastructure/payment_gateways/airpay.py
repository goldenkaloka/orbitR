import logging
import requests
import json
import hashlib
import base64
from django.conf import settings
from django.core.cache import cache
from .base import PaymentGateway
from .airpay_utils import AirpayTransformer

logger = logging.getLogger(__name__)

class AirpayGateway(PaymentGateway):
    """
    Airpay Tanzania Implementation
    Supports OAuth2 Auth, Encrypted Collections, Split Settlement, and Refunds.
    """
    def __init__(self):
        self.config = settings.AIRPAY_CONFIG
        self.sandbox = self.config.get('SANDBOX', True)
        self.sandbox = self.config.get('SANDBOX', True)
        
        if self.sandbox:
            self.base_url = "https://kraken.airpay.tz/airpay/pay/v1/api"
            self.payment_url = "https://payments.airpay.tz/pay/v1"
            self.split_url = "https://kraken.airpay.tz/airpay/neft/split_payout/transaction_level_split_config_api.php"
        else:
            self.base_url = "https://kraken.airpay.tz/airpay/pay/v1/api"
            self.payment_url = "https://payments.airpay.tz/pay/v1"
            self.split_url = "https://kraken.airpay.tz/airpay/neft/split_payout/transaction_level_split_config_api.php"

    def _get_encryption_key(self):
        """
        Derives the AES encryption key as per docs:
        md5(username . "~:~" . password)
        """
        username = self.config.get('USERNAME')
        password = self.config.get('PASSWORD')
        if not username or not password:
            # Fallback to configured secret key if credentials missing
            return self.config.get('SECRET_KEY')
        
        key_str = f"{username}~:~{password}"
        return hashlib.md5(key_str.encode('utf-8')).hexdigest()

    def _get_access_token(self):
        """Standard OAuth2 flow for access token"""
        cached_token = cache.get("airpay_access_token")
        if cached_token:
            return cached_token

        if not self.config.get('CLIENT_ID'):
            return None

        payload = {
            "client_id": self.config.get('CLIENT_ID'),
            "client_secret": self.config.get('CLIENT_SECRET'),
            "merchant_id": self.config.get('MERCHANT_ID'),
            "grant_type": "client_credentials"
        }

        try:
            # Try raw payload first
            response = requests.post(f"{self.base_url}/oauth2", data=payload, timeout=30)
            if response.status_code == 403:
                # Try encrypted if raw failed (Required by some Airpay environments)
                enc_key = self._get_encryption_key()
                encdata = AirpayTransformer.encrypt(json.dumps(payload), enc_key)
                checksum = AirpayTransformer.calculate_checksum(payload)
                auth_payload = {
                    "merchant_id": self.config.get('MERCHANT_ID'),
                    "encdata": encdata,
                    "checksum": checksum
                }
                response = requests.post(f"{self.base_url}/oauth2", data=auth_payload, timeout=30)
            
            response.raise_for_status()
            
            # Response is usually encrypted JSON
            resp_data = response.json()
            if resp_data.get('status') == 'success' or 'response' in resp_data:
                encrypted_resp = resp_data.get('response')
                enc_key = self._get_encryption_key()
                decrypted = AirpayTransformer.decrypt(encrypted_resp, enc_key)
                
                if isinstance(decrypted, dict):
                    token = decrypted.get('access_token')
                    expires = int(decrypted.get('expires_in', 300)) - 30
                    if token:
                        cache.set("airpay_access_token", token, timeout=expires)
                        return token
                
                logger.error(f"Airpay Auth Invalid Response Format: {decrypted}")
            
            logger.error(f"Airpay Auth Failed: {resp_data}")
        except Exception as e:
            logger.error(f"Airpay Auth Exception: {str(e)}")
        
        return None

    def collect(self, amount, phone, order_reference, **kwargs):
        """
        Initiates a payment collection (Simple Transaction).
        Returns the data needed to redirect the user or perform the POST.
        """


        token = self._get_access_token()
        
        # Prepare payload
        # Check if provider is MNO
        chmod_val = 'mmoney' if kwargs.get('provider') in ['Mpesa', 'Tigo', 'Airtel', 'Halopesa'] else ''
        
        data = {
            "merchant_id": str(self.config.get('MERCHANT_ID')),
            "orderid": order_reference,
            "amount": f"{float(amount):.2f}",
            "currency": "834", # TZS
            "isocurrency": "TZS",
            "buyer_email": kwargs.get('email', 'customer@mwanachuo.tz'),
            "buyer_phone": phone,
            "buyer_firstname": kwargs.get('firstname', 'System'),
            "buyer_lastname": kwargs.get('lastname', 'User'),
            "kittype": "server_side_sdk",
            "chmod": chmod_val
        }

        try:
            enc_key = self._get_encryption_key()
            encdata = AirpayTransformer.encrypt(json.dumps(data), enc_key)
            checksum = AirpayTransformer.calculate_checksum(data)
            
            # Airpay requires 'privatekey' for payment initiation
            # Formula: sha256(secret . '@' . username . ':|:' . password)
            pk_base = f"{self.config.get('SECRET_KEY')}@{self.config.get('USERNAME')}:|:{self.config.get('PASSWORD')}"
            privatekey = hashlib.sha256(pk_base.encode('utf-8')).hexdigest()

            endpoint = self.payment_url
            if token:
                endpoint = f"{endpoint}/?token={token}"

            # Generate AES DES Key for native SDK decryption
            aes_des_key = f"{self.config.get('USERNAME')}~:~{self.config.get('PASSWORD')}"

            return {
                'success': True,
                'endpoint': endpoint,
                'params': {
                    'merchant_id': self.config.get('MERCHANT_ID'),
                    'encdata': encdata,
                    'checksum': checksum,
                    'privatekey': privatekey,
                    'client_id': self.config.get('CLIENT_ID'),
                    'client_secret': self.config.get('CLIENT_SECRET'),
                    'aesDeskey': aes_des_key,
                    'domain': 'https://kraken.airpay.tz/',
                    'chmod': 'mmoney' if kwargs.get('provider') in ['Mpesa', 'Tigo', 'Airtel', 'Halopesa'] else ''
                }
            }
        except Exception as e:
            logger.error(f"Airpay Collection Exception: {str(e)}")
            return {'success': False, 'error': str(e)}

    def verify_transaction(self, order_reference=None, ap_transactionid=None):
        """
        Pull transaction status from Airpay.
        Requires either orderid or ap_transactionid.
        """

        token = self._get_access_token()
        if not token:
            return {'success': False, 'error': 'Authentication failed'}

        data = {}
        if order_reference:
            data['orderid'] = order_reference
        if ap_transactionid:
            data['ap_transactionid'] = ap_transactionid

        try:
            enc_key = self._get_encryption_key()
            encdata = AirpayTransformer.encrypt(json.dumps(data), enc_key)
            checksum = AirpayTransformer.calculate_checksum(data)
            
            pk_base = f"{self.config.get('SECRET')}@{self.config.get('USERNAME')}:|:{self.config.get('PASSWORD')}"
            privatekey = hashlib.sha256(pk_base.encode('utf-8')).hexdigest()

            payload = {
                'merchant_id': self.config.get('MERCHANT_ID'),
                'encdata': encdata,
                'checksum': checksum,
                'privatekey': privatekey
            }

            response = requests.post(f"{self.base_url}/verify/?token={token}", data=payload, timeout=30)
            resp_json = response.json()
            
            if resp_json.get('status') == 'success' and 'response' in resp_json:
                decrypted = AirpayTransformer.decrypt(resp_json.get('response'), enc_key)
                
                if isinstance(decrypted, dict):
                    # Verify decrypted ap_SecureHash
                    if AirpayTransformer.verify_secure_hash(decrypted.get('ap_SecureHash'), decrypted, self.config.get('USERNAME')):
                        return {
                            'success': True,
                            'status': decrypted.get('transaction_payment_status'),
                            'data': decrypted
                        }
                    else:
                        return {'success': False, 'error': 'SecureHash mismatch in verify response'}
                else:
                    logger.error(f"Airpay Verify Invalid Response Format: {decrypted}")
                    return {'success': False, 'error': 'Invalid response format from Airpay'}

            return {'success': False, 'error': resp_json.get('message', 'Verify failed')}
        except Exception as e:
            logger.error(f"Airpay Verify Exception: {str(e)}")
            return {'success': False, 'error': str(e)}

    def split_settlement(self, airpay_id, rrn, splits: list, **kwargs):
        """
        Escrow Release: Distributes funds to multiple parties.
        'splits' should be list of dicts: {'identifier': '...', 'value': 10.0}
        """


        # Split settlement needs a special HMAC token
        request_id = kwargs.get('request_id', airpay_id)
        token = AirpayTransformer.generate_token(
            self.config.get('USERNAME'),
            self.config.get('PASSWORD'),
            self.config.get('SECRET'),
            airpay_id,
            self.config.get('SECRET_KEY')
        )

        # Logic: First split in list is Primary Account
        if not splits:
            return {'success': False, 'error': 'No splits provided'}

        primary = splits[0]
        others = splits[1:]

        payload = {
            "TRANSACTION_ID": airpay_id,
            "RRN": rrn,
            "SPLIT_REQUEST_ID": request_id,
            "PRIACCT_IDENTIFIER": primary['identifier'],
            "PRIACCT_SPLIT_VALUE": primary['value'],
            "OTHER_IDENTIFIERS": "|".join(s['identifier'] for s in others),
            "SPLIT_VALUES": "|".join(str(s['value']) for s in others),
            "TOKEN": token
        }

        try:
            # Split API uses API-Key header
            headers = {
                "Content-Type": "application/json",
                "Api-Key": self.config.get('SECRET_KEY')
            }
            response = requests.post(self.split_url, json=payload, headers=headers, timeout=30)
            data = response.json()
            
            if data.get('errorCode') == '000':
                return {'success': True, 'message': 'Split Successful'}
            
            return {'success': False, 'error': data.get('message', 'Split failed'), 'code': data.get('errorCode')}
        except Exception as e:
            logger.error(f"Airpay Split Exception: {str(e)}")
            return {'success': False, 'error': str(e)}

    def get_status(self, provider_reference):
        """Query status of a transaction (Abstract method implementation)"""
        return self.verify_transaction(ap_transactionid=provider_reference)

    def disburse(self, amount, account_info, reference, **kwargs):
        """
        Single disbursement (Abstract method implementation).
        For Airpay, we primarily use split_settlement for escrow release.
        """
        # This could be mapped to a payout API if needed.
        return {'success': False, 'error': 'Single disbursement not implemented for Airpay gateway. Use split_settlement.'}

    def refund(self, airpay_id, amount):
        """
        Escrow Refund: Calls the refund API.
        """

        token = self._get_access_token()
        
        # Base64 encoded JSON list of transactions
        refund_data = [{"ap_transactionid": airpay_id, "amount": f"{float(amount):.2f}"}]
        refund_json_b64 = base64.b64encode(json.dumps(refund_data).encode('utf-8')).decode('utf-8')

        data = {
            "mode": "refund",
            "transactions": refund_json_b64
        }

        try:
            enc_key = self._get_encryption_key()
            encdata = AirpayTransformer.encrypt(json.dumps(data), enc_key)
            checksum = AirpayTransformer.calculate_checksum(data)
            
            # Refund also needs the hash-based privatekey
            pk_base = f"{self.config.get('SECRET')}@{self.config.get('USERNAME')}:|:{self.config.get('PASSWORD')}"
            privatekey = hashlib.sha256(pk_base.encode('utf-8')).hexdigest()

            payload = {
                "merchant_id": self.config.get('MERCHANT_ID'),
                "encdata": encdata,
                "checksum": checksum,
                "privatekey": privatekey
            }

            response = requests.post(f"{self.base_url}/refund/?token={token}", data=payload, timeout=30)
            # Response handling...
            resp_data = response.json()
            if resp_data.get('status') == 'Success':
                # Detailed check in 'data' field
                trans = resp_data.get('data', {}).get('transactions', [])
                if trans and trans[0].get('success') == 'true':
                    return {'success': True, 'refund_id': trans[0].get('refund_id')}
                
            return {'success': False, 'error': resp_data.get('message', 'Refund failed')}
        except Exception as e:
            logger.error(f"Airpay Refund Exception: {str(e)}")
            return {'success': False, 'error': str(e)}
