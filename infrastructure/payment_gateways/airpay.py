import hashlib
import requests
import logging
import json
import base64
import datetime
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
        
        # Endpoints
        self.oauth_url = "https://kraken.airpay.tz/airpay/pay/v1/api/oauth2/"
        self.seamless_url = "https://kraken.airpay.tz/airpay/pay/v1/api/seamless/index.php"
        self.verify_url = "https://kraken.airpay.tz/airpay/pay/v1/api/verify"
        
        self.transformer = AirpayTransformer()

    def collect(self, amount, phone, order_reference, **kwargs):
        """
        Initiates a USSD Push (Seamless) request directly to Airpay Kraken.
        This removes the need for frontend redirects and triggers the STK PIN prompt.
        """
        try:
            formatted_amount = "{:.2f}".format(float(amount))
            token = self._get_token()
            if not token:
                return {'success': False, 'error': 'Failed to obtain Airpay authentication token'}

            # 1. Prepare Seamless Payload
            sanitized_order_id = str(order_reference).replace('-', '')[:30]
            bank_code = kwargs.get('bank_code') or self._get_bank_code(phone)
            
            payload = {
                'buyer_email': kwargs.get('buyer_email', 'customer@mwanachuo.tz'),
                'buyer_phone': phone,
                'buyer_firstname': kwargs.get('buyer_firstname', 'Mwanachuo'),
                'buyer_lastname': kwargs.get('buyer_lastname', 'User'),
                'orderid': sanitized_order_id,
                'amount': formatted_amount,
                'channel': 'mmoney',
                'chmod': 'mmoney',
                'bankcode': bank_code,
                'currency_code': '834',
                'iso_currency': 'TZS',
                'merchant_id': self.mercid
            }
            
            # 2. Generate Security Keys
            private_key = self.transformer.encrypt_sha(self.username + ":|:" + self.password, self.secret)
            enc_data = self.transformer.encrypt_string(json.dumps(payload), self.username, self.password)
            checksum = self.transformer.checksum_cal(payload)
            
            # 3. Execute Push Request (Server-to-Server)
            post_data = {
                'merchant_id': self.mercid,
                'privatekey': private_key,
                'encdata': enc_data,
                'checksum': checksum
            }
            
            response = requests.post(
                f"{self.seamless_url}?token={token}",
                data=post_data,
                timeout=15
            )
            
            if response.status_code == 200:
                try:
                    res_json = response.json()
                except json.JSONDecodeError:
                    # Clean response text if it has trailing junk
                    text = response.text.strip()
                    last_brace = text.rfind('}')
                    res_json = json.loads(text[:last_brace+1])
                # Airpay returns an encrypted 'response' field even for seamless
                if 'response' in res_json:
                    decrypted = self.transformer.decrypt_string(res_json['response'], self.username, self.password)
                    res_data = json.loads(decrypted)
                    
                    # Status 200 means the request was accepted and push sent
                    if res_data.get('status_code') == '200' or res_data.get('status') == 'success':
                        return {
                            'success': True, 
                            'provider_reference': res_data.get('data', {}).get('ap_transactionid'),
                            'message': 'USSD Push initiated successfully',
                            'raw_data': res_data
                        }
                    
                    error_msg = res_data.get('message') or "USSD Push rejected by provider"
                    return {'success': False, 'error': error_msg, 'code': res_data.get('response_code')}
                
                return {'success': False, 'error': f"Unexpected response format: {response.text}"}
            
            return {'success': False, 'error': f"Airpay API Error: {response.status_code}"}

        except Exception as e:
            logger.error(f"Airpay USSD Push Error: {str(e)}")
            return {'success': False, 'error': str(e)}

    def _get_bank_code(self, phone):
        """
        Determines the Tanzania mobile operator based on phone prefix.
        Defaults to MPESA if unknown.
        """
        p = str(phone).replace('+', '')
        if p.startswith('255'): p = p[3:]
        if p.startswith('0'): p = p[1:]
        
        prefix = p[:2]
        # Vodacom: 74, 75, 76, 73
        if prefix in ['74', '75', '76', '73']: return 'MPESA'
        # Tigo: 65, 67, 71, 72
        if prefix in ['65', '67', '71', '72']: return 'TIGO'
        # Airtel: 68, 69, 78, 79
        if prefix in ['68', '69', '78', '79']: return 'AIRTEL'
        # Halotel: 61, 62
        if prefix in ['61', '62']: return 'HALOPESA'
        
        return 'MPESA' # Fallback

    def verify(self, response_data):
        """
        Verifies the response from Airpay callback.
        Supports both direct responses and standard IPN notifications.
        """
        # If response is encrypted (v4 style)
        if 'response' in response_data:
            try:
                decrypted_json = self.transformer.decrypt_string(response_data['response'], self.username, self.password)
                data_dict = json.loads(decrypted_json)
                actual_data = data_dict.get('data', data_dict)
                if self.transformer.verify_checksum_v4(actual_data, self.mercid, self.username):
                    return actual_data.get('transaction_status') == '200'
            except Exception as e:
                logger.error(f"Airpay Callback Decryption Error: {str(e)}")
                return False
        
        # Standard IPN (Direct keys)
        return self.transformer.verify_checksum_v4(response_data, self.mercid, self.username)

    def get_status(self, provider_reference=None, orderid=None):
        """
        Queries the transaction status using the Verify API.
        """
        try:
            token = self._get_token()
            if not token: return {'success': False, 'error': 'No token'}

            data = {}
            if orderid: data['orderid'] = orderid
            if provider_reference: data['ap_transactionid'] = provider_reference

            private_key = self.transformer.encrypt_sha(self.username + ":|:" + self.password, self.secret)
            enc_data = self.transformer.encrypt_string(json.dumps(data), self.username, self.password)
            checksum = self.transformer.checksum_cal(data)

            payload = {
                'merchant_id': self.mercid,
                'encdata': enc_data,
                'checksum': checksum,
                'privatekey': private_key
            }

            response = requests.post(f"{self.verify_url}?token={token}", data=payload, timeout=10)
            if response.status_code == 200:
                try:
                    res_json = response.json()
                except json.JSONDecodeError:
                    text = response.text.strip()
                    last_brace = text.rfind('}')
                    res_json = json.loads(text[:last_brace+1])
                if 'response' in res_json:
                    decrypted = self.transformer.decrypt_string(res_json['response'], self.username, self.password)
                    res_data = json.loads(decrypted)
                    actual_data = res_data.get('data', {})
                    
                    status = actual_data.get('transaction_status')
                    return {
                        'success': True,
                        'status': 'SUCCESS' if status == '200' else 'FAILED',
                        'raw_status': status,
                        'data': actual_data
                    }
            return {'success': False, 'error': f"Verify failed: {response.text}"}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def disburse(self, amount, account_info, reference, **kwargs):
        return {'success': False, 'error': 'B2C disbursements not supported via this Airpay MID'}

    def refund(self, transaction_id, amount):
        """
        Initiates a refund for a transaction.
        """
        try:
            token = self._get_token()
            refund_data = [{
                "ap_transactionid": transaction_id,
                "amount": "{:.2f}".format(float(amount))
            }]
            
            data = {
                'transactions': base64.b64encode(json.dumps(refund_data).encode()).decode()
            }

            private_key = self.transformer.encrypt_sha(self.username + ":|:" + self.password, self.secret)
            enc_data = self.transformer.encrypt_string(json.dumps(data), self.username, self.password)
            checksum = self.transformer.checksum_cal(data)

            payload = {
                'merchant_id': self.mercid,
                'encdata': enc_data,
                'checksum': checksum,
                'privatekey': private_key
            }

            response = requests.post(f"https://kraken.airpay.tz/airpay/pay/v1/api/refund?token={token}", data=payload)
            return {'success': response.status_code == 200, 'data': response.json() if response.status_code == 200 else response.text}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def _get_token(self):
        """
        Fetches OAuth2 token from Kraken Tanzania.
        """
        cache_key = f"airpay_token_{self.mercid}"
        token = cache.get(cache_key)
        if token:
            return token
        
        # Calculate Checksum for OAuth
        current_date = datetime.datetime.now().strftime("%Y-%m-%d")
        all_data = f"{self.client_id}{self.client_secret}client_credentials{self.mercid}{current_date}"
        checksum = hashlib.sha256(all_data.encode()).hexdigest()
        
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
            "merchant_id": int(self.mercid)
        }
        enc_data = self.transformer.encrypt_string(json.dumps(payload), self.username, self.password)
        
        try:
            response = requests.post(self.oauth_url, data={
                'merchant_id': self.mercid,
                'checksum': checksum,
                'encdata': enc_data
            }, timeout=10)
            
            if response.status_code == 200:
                try:
                    res_json = response.json()
                except json.JSONDecodeError:
                    text = response.text.strip()
                    last_brace = text.rfind('}')
                    res_json = json.loads(text[:last_brace+1])
                if 'response' in res_json:
                    decrypted = self.transformer.decrypt_string(res_json['response'], self.username, self.password)
                    res_data = json.loads(decrypted)
                    
                    # Status check can be 'success' or code '200'
                    if res_data.get('status') == 'success' or res_data.get('status_code') == '200':
                        data = res_data.get('data', res_data)
                        token = data.get('access_token')
                        if token:
                            cache.set(cache_key, token, 3500)
                            return token
            logger.error(f"Airpay Auth Failed: {response.text}")
            return ""
        except Exception as e:
            logger.error(f"Airpay Token Error: {str(e)}")
            return ""
