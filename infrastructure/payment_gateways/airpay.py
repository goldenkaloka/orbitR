import hashlib
import requests
import logging
import json
import base64
from django.conf import settings
from django.core.cache import cache
from .base import PaymentGateway
from .airpay_utils import AirpayTransformer

logger = logging.getLogger(__name__)

class AirpayGateway(PaymentGateway):
    """
    Airpay Tanzania Implementation (Official v4 SDK Support)
    """
    def __init__(self):
        self.config = settings.AIRPAY_CONFIG
        self.mercid = self.config.get('MERCHANT_ID')
        self.username = self.config.get('USERNAME')
        self.password = self.config.get('PASSWORD')
        self.secret = self.config.get('SECRET_KEY')
        self.client_id = self.config.get('CLIENT_ID')
        self.client_secret = self.config.get('CLIENT_SECRET')
        self.merchant_domain = self.config.get('MERCHANT_DOMAIN', 'mwanachuo.tz').replace('https://', '').replace('http://', '').split('/')[0]
        
        self.payment_url = 'https://payments.airpay.tz/pay/v1/index.php'
        self.embedded_url = 'https://payments.airpay.tz/pay/v1/embedded'
        
        self.transformer = AirpayTransformer()

    def collect(self, amount, phone, order_reference, **kwargs):
        """
        Generates parameters for Airpay transaction.
        Optimized for the Airpay Flutter SDK v4.
        """
        try:
            formatted_amount = "{:.2f}".format(float(amount))
            
            # 1. Fetch OAuth Token (Required for Embedded v1)
            token = self._get_token()
            
            # 2. Prepare Data for Encryption
            # Sanitize order_reference for Airpay (Alphanumeric 1-30)
            sanitized_order_id = str(order_reference).replace('-', '')[:20]
            
            payload = {
                'orderid': sanitized_order_id,
                'amount': formatted_amount,
                'currency_code': '834',
                'iso_currency': 'tzs',
                'buyer_email': kwargs.get('buyer_email', 'customer@mwanachuo.tz'),
                'buyer_phone': phone,
                'buyer_firstname': kwargs.get('buyer_firstname', 'Mwanachuo'),
                'buyer_lastname': kwargs.get('buyer_lastname', 'User'),
                'buyer_address': kwargs.get('buyer_address', 'Dar es Salaam'),
                'buyer_city': kwargs.get('buyer_city', 'Dar es Salaam'),
                'buyer_state': kwargs.get('buyer_state', 'Dar es Salaam'),
                'buyer_country': 'Tanzania',
                'buyer_pincode': '0000',
                'chmod': kwargs.get('chmod', ''), # Blank for all options
                'walletflg': 'N',
                'channel': 'pg',
                'txnsubtype': '2',
                'mer_dom': base64.b64encode(self.merchant_domain.encode()).decode()
            }
            
            # 3. Generate Security Keys
            private_key = self.transformer.encrypt_sha(self.username + ":|:" + self.password, self.secret)
            enc_data = self.transformer.encrypt_string(json.dumps(payload), self.username, self.password)
            checksum = self.transformer.checksum_cal(payload)
            
            return {
                'success': True,
                'params': {
                    'merchant_id': self.mercid,
                    'privatekey': private_key,
                    'encdata': enc_data,
                    'checksum': checksum,
                    'token': token,
                    'order_id': order_reference, # For UI/Tracking
                    'success_url': 'https://mwanachuo.tz/payment/success',
                    'failed_url': 'https://mwanachuo.tz/payment/failed',
                }
            }

        except Exception as e:
            logger.error(f"Airpay Initiation Error: {str(e)}")
            return {'success': False, 'error': str(e)}

    def verify(self, response_data):
        """
        Verifies the response from Airpay callback.
        """
        if 'response' in response_data:
            try:
                decrypted_json = self.transformer.decrypt_string(response_data['response'], self.username, self.password)
                data_dict = json.loads(decrypted_json)
                actual_data = data_dict.get('data', {})
                if self.transformer.verify_checksum_v4(actual_data, self.mercid, self.username):
                    return actual_data.get('transaction_status') == '200'
            except Exception as e:
                logger.error(f"Airpay Decryption/Verification Error: {str(e)}")
                return False
        
        return self.transformer.verify_checksum_v4(response_data, self.mercid, self.username)

    def disburse(self, amount, account_info, reference, **kwargs):
        logger.warning(f"Airpay Disburse called for {reference}, but not yet implemented.")
        return {'success': False, 'error': 'Split settlement / Disbursement not yet implemented for Airpay v4'}

    def get_status(self, provider_reference):
        logger.warning(f"Airpay Get Status called for {provider_reference}, but not yet implemented.")
        return {'success': False, 'error': 'Status query not yet implemented'}

    def refund(self, transaction_id, amount):
        return {'success': False, 'error': 'Refund not yet implemented'}

    def _get_token(self):
        """
        Fetches OAuth2 token from Kraken Tanzania.
        """
        cache_key = f"airpay_token_{self.mercid}"
        token = cache.get(cache_key)
        if token:
            return token

        import datetime
        from django.utils import timezone
        
        oauth_url = "https://kraken.airpay.tz/airpay/pay/v1/api/oauth2/"
        
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "merchant_id": int(self.mercid)
        }
        
        # Calculate Checksum for OAuth (Alphabetical: client_id, client_secret, grant_type, merchant_id)
        # Use System Local Time (matching the successful test_airpay_token.py)
        current_date = datetime.datetime.now().strftime("%Y-%m-%d")
        all_data = f"{self.client_id}{self.client_secret}client_credentials{self.mercid}{current_date}"
        checksum = hashlib.sha256(all_data.encode()).hexdigest()
        
        # Encrypt Payload
        enc_data = self.transformer.encrypt_string(json.dumps(payload), self.username, self.password)
        
        try:
            response = requests.post(oauth_url, data={
                'merchant_id': self.mercid,
                'checksum': checksum,
                'encdata': enc_data
            }, timeout=10)
            
            if response.status_code == 200:
                res_json = response.json()
                if 'response' in res_json:
                    decrypted = self.transformer.decrypt_string(res_json['response'], self.username, self.password)
                    res_data = json.loads(decrypted)
                    
                    if res_data.get('status') == 'success' or res_data.get('data', {}).get('status') == 'success':
                        token = res_data.get('data', {}).get('access_token')
                        if token:
                            cache.set(cache_key, token, 3500)
                            return token
                    
                    # Detailed Error Logging
                    error_msg = res_data.get('data', {}).get('message') or res_data.get('message')
                    logger.error(f"Airpay Auth Failed: {error_msg} (Ref: {res_data.get('data', {}).get('error_code')})")
                else:
                    logger.error(f"Airpay Response missing 'response' field: {res_json}")
            else:
                logger.error(f"Airpay Kraken HTTP {response.status_code}: {response.text}")
            return ""
        except Exception as e:
            logger.error(f"Airpay Token Error: {str(e)}")
            return ""
