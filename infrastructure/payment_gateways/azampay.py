import logging
import requests
import time
import base64
import hashlib
from django.conf import settings
from django.core.cache import cache
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from .base import PaymentGateway

logger = logging.getLogger(__name__)

class AzamPayGateway(PaymentGateway):
    def __init__(self):
        self.config = settings.AZAMPAY_CONFIG
        self.sandbox = self.config.get('SANDBOX', True)
        
        # Base URLs based on environment
        if self.sandbox:
            self.auth_url = "https://authenticator-sandbox.azampay.co.tz/AppRegistration/GenerateToken"
            self.checkout_url = "https://sandbox.azampay.co.tz/azampay/mno/checkout"
            self.disburse_url = "https://api-disbursement-sandbox.azampay.co.tz/api/v1/azampay/disburse"
            self.public_key_url = "https://sandbox.azampay.co.tz/azampay/v1/public-key?format=Pem"
        else:
            self.auth_url = "https://authenticator.azampay.co.tz/AppRegistration/GenerateToken"
            self.checkout_url = "https://api.azampay.co.tz/azampay/mno/checkout"
            self.disburse_url = "https://api-disbursement.azampay.co.tz/api/v1/azampay/disburse"
            self.public_key_url = "https://api.azampay.co.tz/azampay/v1/public-key?format=Pem"

        self._token = None
        self._token_expiry = 0
        self._cached_public_key = None

    def _get_token(self):
        """Fetch or return cached JWT token"""
        cached_token = cache.get("azampay_token")
        if cached_token:
            return cached_token

        payload = {
            "appName": self.config.get('APP_NAME'),
            "clientId": self.config.get('CLIENT_ID'),
            "clientSecret": self.config.get('CLIENT_SECRET')
        }
        
        try:
            response = requests.post(self.auth_url, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            if data.get('success'):
                token = data['data']['accessToken']
                # Token usually valid for 3600s; we'll cache at 3500s
                cache.set("azampay_token", token, timeout=3500)
                logger.info("AzamPay manual token generated successfully")
                return token
            else:
                logger.error(f"AzamPay Auth Failed: {data.get('message')}")
        except Exception as e:
            logger.error(f"AzamPay Auth Exception: {str(e)}")
        
        return None

    def _get_headers(self, include_api_key=False):
        token = self._get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        if include_api_key and self.config.get('API_KEY'):
            headers["X-API-Key"] = self.config.get('API_KEY')
        return headers

    def collect(self, amount, phone, order_reference, **kwargs):
        """Standard MNO Collection (Checkout)"""
        if self.config.get('MOCK', False):
            return {'success': True, 'transactionId': f'MOCK_TXN_{order_reference}'}

        provider = self._map_mno(kwargs.get('provider', 'Tigo'))
        
        payload = {
            "accountNumber": phone,
            "amount": float(amount),
            "currency": "TZS",
            "externalId": order_reference,
            "provider": provider,
            "additionalProperties": kwargs.get('properties', {})
        }

        try:
            headers = self._get_headers(include_api_key=True)
            response = requests.post(self.checkout_url, json=payload, headers=headers, timeout=40)
            data = response.json()
            
            if data.get('success'):
                return {'success': True, 'transactionId': data.get('transactionId'), 'raw': data}
            
            logger.error(f"AzamPay Collection Rejected: {data}")
            return {'success': False, 'message': data.get('message'), 'raw': data}
        except Exception as e:
            logger.error(f"AzamPay Collection Exception: {str(e)}")
            return {'success': False, 'error': str(e)}

    def disburse(self, amount, account_info=None, reference=None, **kwargs):
        """3-Way Financial Distribution (Payout)"""
        # Maintain compatibility with callers using 'phone' or 'reference' as keywords
        phone = account_info or kwargs.get('phone')
        ref = reference or kwargs.get('reference')
        
        if not phone:
            logger.error("AzamPay Disbursement Failed: No phone/account_info provided.")
            return {'success': False, 'error': 'Missing account information'}

        if self.config.get('MOCK', False):
            return {'success': True, 'transactionId': f'MOCK_DISB_{ref}'}

        # Checksum calculation: Base64(RSA(SHA512(string)))
        source_name = self.config.get('APP_NAME')
        # Placeholder concatenation: Source + Destination + Amount + Ref
        checksum_string = f"{source_name}{phone}{amount}{ref}"
        checksum = self._calculate_disburse_checksum(checksum_string)

        payload = {
            "source": {
                "countryCode": "TZ",
                "fullName": source_name,
                "bankName": "AzamPay", 
                "accountNumber": self.config.get('CLIENT_ID'), # App/Merchant Account
                "currency": "TZS"
            },
            "destination": {
                "countryCode": "TZ",
                "fullName": kwargs.get('recipient_name', 'System User'),
                "bankName": self._map_mno(kwargs.get('provider', 'Tigo')),
                "accountNumber": phone,
                "currency": "TZS"
            },
            "transferDetails": {
                "type": "Mobile",
                "amount": float(amount),
                "dateInEpoch": int(time.time())
            },
            "externalReferenceId": ref,
            "remarks": kwargs.get('remarks', 'Mwanachuo Disbursement'),
            "checksum": checksum
        }

        try:
            headers = self._get_headers()
            response = requests.post(self.disburse_url, json=payload, headers=headers, timeout=30)
            data = response.json()
            
            if data.get('success'):
                return {'success': True, 'transactionId': data.get('transactionId'), 'raw': data}
            
            logger.error(f"AzamPay Disbursement Rejected: {data}")
            return {'success': False, 'message': str(data), 'raw': data}
        except Exception as e:
            logger.error(f"AzamPay Disbursement Exception: {str(e)}")
            return {'success': False, 'error': str(e)}

    def get_status(self, provider_reference):
        """Query transaction status via direct API"""
        if self.config.get('MOCK', False):
            return 'Success'

        # Endpoint uses pgReferenceId and bankName
        # Note: In Sandbox, we might not always have the bankName handy during a raw status check,
        # but the API requires it. We'll default to Tigo for lookups if not provided.
        params = {
            "pgReferenceId": provider_reference,
            "bankName": "Tigo" 
        }

        try:
            headers = self._get_headers()
            # The spec suggests api-disbursement base for this
            status_url = f"{self.disburse_url.replace('/disburse', '/transactionstatus')}"
            response = requests.get(status_url, params=params, headers=headers, timeout=10)
            data = response.json()
            
            # The spec shows 200/201/401/402. We'll look for transactionstatus in the body
            return data.get('transactionstatus', 'Unknown')
        except Exception as e:
            logger.error(f"AzamPay Status Check Failed: {str(e)}")
            return 'Unknown'

    def verify_callback_signature(self, payload: dict) -> bool:
        """
        Verify RSA signature from AzamPay callback:
        Concatenate: {utilityref}{externalreference}{transactionstatus}{operator}
        """
        signature_b64 = payload.get('signature')
        if not signature_b64:
            return False

        # Build message data for verification
        msg = f"{payload.get('utilityref','')}{payload.get('externalreference','')}{payload.get('transactionstatus','')}{payload.get('operator','')}"
        
        try:
            public_key = self.get_public_key()
            if not public_key:
                return False

            public_key.verify(
                base64.b64decode(signature_b64),
                msg.encode('utf-8'),
                padding.PKCS1v15(),
                hashes.SHA256()
            )
            return True
        except Exception as e:
            logger.error(f"AzamPay Signature Verification Failed: {str(e)}")
            return False

    def get_public_key(self):
        """Fetch and parse AzamPay Public Key"""
        cached_key = cache.get("azampay_public_key")
        if cached_key:
            return cached_key

        try:
            # Include API Key in case it's required for this endpoint
            headers = self._get_headers(include_api_key=True)
            response = requests.get(self.public_key_url, headers=headers, timeout=10)
            data = response.json()
            if data.get('success') and data.get('publicKey'):
                key_text = data['publicKey']
                public_key = serialization.load_pem_public_key(key_text.encode('utf-8'))
                cache.set("azampay_public_key", public_key, timeout=86400)
                return public_key
        except Exception as e:
            logger.error(f"Failed to fetch AzamPay Public Key: {str(e)}")
        
        return None

    def _calculate_disburse_checksum(self, plain_text: str) -> str:
        """
        Implements Base64(RSA(SHA512(string)))
        Note: Per spec, uses PKCS1 padding
        """
        try:
            # Step 1: SHA512 Hash
            sha512_hash = hashlib.sha512(plain_text.encode('utf-8')).digest()
            
            # Step 2: RSA Encrypt with PKCS1 padding
            public_key = self.get_public_key()
            if not public_key:
                return "signature_placeholder"

            encrypted = public_key.encrypt(
                sha512_hash,
                padding.PKCS1v15() # PKCS1 padding as requested
            )
            
            # Step 3: Base64
            return base64.b64encode(encrypted).decode('utf-8')
        except Exception as e:
            logger.error(f"Checksum Calculation Failed: {str(e)}")
            return "error_calculating_checksum"

    def _map_mno(self, provider_name: str) -> str:
        mapping = {
            "tigo": "Tigo", "airtel": "Airtel", "halopesa": "Halopesa",
            "azampesa": "Azampesa", "mpesa": "Mpesa", "vodacom": "Mpesa"
        }
        return mapping.get(str(provider_name).lower(), "Tigo")
