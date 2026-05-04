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
            
            # 1. Generate core params that the SDK needs
            # The Flutter SDK (UserRequest) needs these to recalculate hashes if needed
            private_key = self.transformer.encrypt_sha(self.username + ":|:" + self.password, self.secret)
            
            # The 'aesDeskey' expected by Flutter SDK is the raw string that gets MD5ed
            aes_des_key = f"{self.username}~:~{self.password}"
            
            # 2. Return parameters for Flutter SDK
            return {
                'success': True,
                'params': {
                    'merchant_id': self.mercid,
                    'privatekey': private_key,
                    'order_id': order_reference,
                    'amount': formatted_amount,
                    'buyer_phone': phone,
                    'buyer_email': kwargs.get('buyer_email', 'customer@mwanachuo.tz'),
                    'buyer_firstname': kwargs.get('buyer_firstname', 'Mwanachuo'),
                    'buyer_lastname': kwargs.get('buyer_lastname', 'User'),
                    'buyer_address': kwargs.get('buyer_address', 'Dar es Salaam'),
                    'buyer_city': kwargs.get('buyer_city', 'Dar es Salaam'),
                    'buyer_state': kwargs.get('buyer_state', 'Dar es Salaam'),
                    'chmod': kwargs.get('chmod', ''),
                    # Extra fields for the official Flutter SDK to handle its own OAuth/Encryption
                    'secret_key': self.secret,
                    'aes_key': aes_des_key,
                    'proto_domain': 'https://payments.airpay.tz/',
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
