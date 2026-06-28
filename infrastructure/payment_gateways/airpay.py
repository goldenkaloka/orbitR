import base64
import datetime
import hashlib
import hmac
import json
import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache

from .airpay_utils import AirpayTransformer
from .base import PaymentGateway

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
        self.merchant_domain = self.config.get('MERCHANT_DOMAIN', 'https://mwanachuoshop.com')
        
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
            # Airpay seamless requires a short numeric order_id — use millisecond timestamp
            import time
            order_id = str(int(time.time() * 1000000))[-15:]
            bank_code = kwargs.get('bank_code') or self._get_bank_code(phone)
            
            payload = {
                'buyer_email': kwargs.get('buyer_email', 'customer@mwanachuo.tz'),
                'buyer_phone': phone,
                'buyer_firstname': kwargs.get('buyer_firstname', 'Mwanachuo'),
                'buyer_lastname': kwargs.get('buyer_lastname', 'User'),
                'orderid': order_id,
                'amount': formatted_amount,
                'channel': 'mmoney',
                'chmod': 'mmoney',
                'bankcode': bank_code,
                'currency_code': '834',
                'iso_currency': 'TZS',
                'merchant_id': self.mercid,
                'mer_dom': base64.b64encode(self.merchant_domain.encode()).decode()
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
                timeout=30
            )
            
            # Helper to clear cache and retry
            def handle_token_failure(reason):
                logger.warning(f"Airpay token validation failed ({reason}). Clearing cache and retrying...")
                cache.delete(f"airpay_token_{self.mercid}")
                if not kwargs.get('is_retry'):
                    kwargs['is_retry'] = True
                    return self.collect(amount, phone, order_reference, **kwargs)
                return {'success': False, 'error': 'Access token validation failed. Please try again.'}

            if response.status_code == 401:
                return handle_token_failure("HTTP 401")

            if response.status_code == 200:
                try:
                    res_json = response.json()
                except json.JSONDecodeError:
                    text = response.text.strip()
                    last_brace = text.rfind('}')
                    res_json = json.loads(text[:last_brace+1])
                
                # Check for unencrypted validation error
                message_str = str(res_json.get('message') or '').lower()
                status_str = str(res_json.get('status') or '').lower()
                if status_str == 'error' and 'validation failed' in message_str:
                    return handle_token_failure("Unencrypted 200 validation error")

                if 'response' in res_json:
                    decrypted = self.transformer.decrypt_string(res_json['response'], self.username, self.password)
                    try:
                        res_data = self.transformer.safe_json_loads(decrypted)
                    except Exception as e:
                        logger.error(f"AIRPAY_DEBUG: Failed to parse decrypted JSON: {str(e)}. String: {decrypted}")
                        return {'success': False, 'error': f"Payment provider returned malformed data: {str(e)}"}
                    
                    # Check if decrypted message contains token validation failure
                    decrypted_msg = str(res_data.get('message') or '').lower()
                    if 'validation failed' in decrypted_msg or 'invalid token' in decrypted_msg:
                        return handle_token_failure("Decrypted 200 validation error")

                    # Status 200 means the request was accepted and push sent
                    if res_data.get('status_code') == '200' or res_data.get('status') == 'success':
                        return {
                            'success': True, 
                            'provider_reference': res_data.get('data', {}).get('ap_transactionid'),
                            'message': 'USSD Push initiated successfully',
                            'raw_data': res_data
                        }
                    
                    logger.error(f"AIRPAY_DEBUG: Push Request Rejected. Decrypted Response: {res_data}")
                    error_msg = res_data.get('message') or "USSD Push rejected by provider"
                    return {'success': False, 'error': error_msg, 'code': res_data.get('response_code')}
                
                # If neither 'response' nor expected error is in res_json, check raw text
                if 'validation failed' in response.text.lower():
                    return handle_token_failure("Raw response text validation error")

                logger.error(f"AIRPAY_DEBUG: Unexpected 200 response format: {response.text}")
                return {'success': False, 'error': "Unexpected response format from Airpay"}
            else:
                # If non-200 check if raw text has validation failed
                if 'validation failed' in response.text.lower():
                    return handle_token_failure(f"HTTP {response.status_code} validation error")
                
                logger.error(f"AIRPAY_DEBUG: Seamless/Push Request Failed. Status: {response.status_code}, Body: {response.text}")
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

    def get_status(self, provider_reference=None, orderid=None, **kwargs):
        """
        Queries the transaction status using the Verify API.
        """
        try:
            is_retry = kwargs.get('is_retry', False)
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

            response = requests.post(f"{self.verify_url}?token={token}", data=payload, timeout=30)
            
            # Helper to clear cache and retry
            def handle_token_failure(reason):
                logger.warning(f"Airpay verify token validation failed ({reason}). Clearing cache and retrying...")
                cache.delete(f"airpay_token_{self.mercid}")
                if not is_retry:
                    kwargs['is_retry'] = True
                    return self.get_status(provider_reference, orderid, **kwargs)
                return {'success': False, 'error': 'Access token validation failed. Please try again.'}

            if response.status_code == 401 or 'validation failed' in response.text.lower():
                return handle_token_failure(f"HTTP {response.status_code}")

            if response.status_code == 200:
                try:
                    res_json = response.json()
                except json.JSONDecodeError:
                    text = response.text.strip()
                    last_brace = text.rfind('}')
                    res_json = json.loads(text[:last_brace+1])
                
                # Check for unencrypted validation error
                message_str = str(res_json.get('message') or '').lower()
                status_str = str(res_json.get('status') or '').lower()
                if status_str == 'error' and 'validation failed' in message_str:
                    return handle_token_failure("Unencrypted verify response validation error")

                if 'response' in res_json:
                    decrypted = self.transformer.decrypt_string(res_json['response'], self.username, self.password)
                    res_data = self.transformer.safe_json_loads(decrypted)
                    
                    # Check if decrypted message contains token validation failure
                    decrypted_msg = str(res_data.get('message') or '').lower()
                    if 'validation failed' in decrypted_msg or 'invalid token' in decrypted_msg:
                        return handle_token_failure("Decrypted verify response validation error")

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
        """
        Disburse funds to a vendor/driver mobile money via Airpay Kraken payout API.
        Follows the same encryption/checksum/auth pattern as other Kraken endpoints.
        """
        try:
            token = self._get_token()
            if not token:
                return {'success': False, 'error': 'Failed to obtain Airpay authentication token'}

            phone = account_info
            formatted_amount = "{:.2f}".format(float(amount))
            payout_url = self.config.get('PAYOUT_URL') or "https://kraken.airpay.tz/airpay/pay/v1/api/disburse"

            # Build payout payload — same pattern as other Kraken APIs
            payload = {
                'phone': phone,
                'amount': formatted_amount,
                'reference': str(reference)[:30],
                'merchant_id': self.mercid,
                'currency_code': '834',
                'iso_currency': 'TZS',
                'narration': kwargs.get('narration', 'Wallet Cashout'),
            }

            private_key = self.transformer.encrypt_sha(self.username + ":|:" + self.password, self.secret)
            enc_data = self.transformer.encrypt_string(json.dumps(payload), self.username, self.password)
            checksum = self.transformer.checksum_cal(payload)

            post_data = {
                'merchant_id': self.mercid,
                'privatekey': private_key,
                'encdata': enc_data,
                'checksum': checksum,
            }

            response = requests.post(
                f"{payout_url}?token={token}",
                data=post_data,
                timeout=30
            )

            def handle_token_failure(reason):
                logger.warning(f"Airpay disbursement token validation failed ({reason}). Clearing cache and retrying...")
                cache.delete(f"airpay_token_{self.mercid}")
                if not kwargs.get('is_retry'):
                    kwargs['is_retry'] = True
                    return self.disburse(amount, account_info, reference, **kwargs)
                return {'success': False, 'error': 'Access token validation failed. Please try again.'}

            if response.status_code == 401:
                return handle_token_failure("HTTP 401")

            if response.status_code == 200:
                try:
                    res_json = response.json()
                except json.JSONDecodeError:
                    text = response.text.strip()
                    last_brace = text.rfind('}')
                    res_json = json.loads(text[:last_brace + 1])

                message_str = str(res_json.get('message') or '').lower()
                status_str = str(res_json.get('status') or '').lower()
                if status_str == 'error' and 'validation failed' in message_str:
                    return handle_token_failure("Unencrypted 200 validation error")

                if 'response' in res_json:
                    decrypted = self.transformer.decrypt_string(res_json['response'], self.username, self.password)
                    res_data = self.transformer.safe_json_loads(decrypted)

                    decrypted_msg = str(res_data.get('message') or '').lower()
                    if 'validation failed' in decrypted_msg or 'invalid token' in decrypted_msg:
                        return handle_token_failure("Decrypted 200 validation error")

                    if res_data.get('status_code') == '200' or res_data.get('status') == 'success':
                        return {
                            'success': True,
                            'provider_reference': res_data.get('data', {}).get('ap_transactionid') or res_data.get('data', {}).get('transaction_id'),
                            'message': 'Disbursement successful',
                            'raw_data': res_data,
                        }

                    logger.error(f"Disbursement rejected. Response: {res_data}")
                    error_msg = res_data.get('message') or "Disbursement rejected by provider"
                    return {'success': False, 'error': error_msg, 'code': res_data.get('response_code')}

                logger.error(f"Unexpected 200 response: {response.text}")
                return {'success': False, 'error': 'Unexpected response format from Airpay'}

            logger.error(f"Disbursement request failed. Status: {response.status_code}, Body: {response.text}")
            return {'success': False, 'error': f"Airpay API Error: {response.status_code}"}

        except Exception as e:
            logger.error(f"Airpay disbursement error: {str(e)}")
            return {'success': False, 'error': str(e)}

    def refund(self, transaction_id, amount, **kwargs):
        """
        Initiates a refund for a transaction.
        """
        try:
            is_retry = kwargs.get('is_retry', False)
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

            response = requests.post(f"https://kraken.airpay.tz/airpay/pay/v1/api/refund?token={token}", data=payload, timeout=30)
            
            # Helper to clear cache and retry
            def handle_token_failure(reason):
                logger.warning(f"Airpay refund token validation failed ({reason}). Clearing cache and retrying...")
                cache.delete(f"airpay_token_{self.mercid}")
                if not is_retry:
                    kwargs['is_retry'] = True
                    return self.refund(transaction_id, amount, **kwargs)
                return {'success': False, 'error': 'Access token validation failed. Please try again.'}

            if response.status_code == 401 or 'validation failed' in response.text.lower():
                return handle_token_failure(f"HTTP {response.status_code}")

            if response.status_code == 200:
                try:
                    res_json = response.json()
                except json.JSONDecodeError:
                    text = response.text.strip()
                    last_brace = text.rfind('}')
                    res_json = json.loads(text[:last_brace+1])
                
                # Check for unencrypted validation error
                message_str = str(res_json.get('message') or '').lower()
                status_str = str(res_json.get('status') or '').lower()
                if status_str == 'error' and 'validation failed' in message_str:
                    return handle_token_failure("Unencrypted refund response validation error")

                # Decrypt and check inside response if possible
                if 'response' in res_json:
                    decrypted = self.transformer.decrypt_string(res_json['response'], self.username, self.password)
                    res_data = self.transformer.safe_json_loads(decrypted)
                    decrypted_msg = str(res_data.get('message') or '').lower()
                    if 'validation failed' in decrypted_msg or 'invalid token' in decrypted_msg:
                        return handle_token_failure("Decrypted refund response validation error")

            return {'success': response.status_code == 200, 'data': response.json() if response.status_code == 200 else response.text}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def split_settlement(self, airpay_id, rrn, primary_account=None, primary_value=None, other_identifiers=None, split_values=None, splits=None, split_request_id=None):
        """
        Release escrowed funds via Airpay split settlement API.

        Per Airpay docs:
          PRIACCT_IDENTIFIER  = platform primary bank account identifier
          PRIACCT_SPLIT_VALUE = platform's share (absolute TZS amount)
          OTHER_IDENTIFIERS   = pipe-separated vendor/driver phone numbers
          SPLIT_VALUES        = pipe-separated amounts matching OTHER_IDENTIFIERS

        POST /airpay/neft/split_payout/transaction_level_split_config_api.php
        """
        api_key = self.config.get('SPLIT_API_KEY')
        if not primary_account:
            primary_account = self.config.get('PRIMARY_ACCOUNT')

        if not api_key or not primary_account:
            logger.error("Split settlement not configured: missing SPLIT_API_KEY or PRIMARY_ACCOUNT")
            return {'success': False, 'error': 'Split settlement not configured'}

        # Support legacy callers that pass a splits list
        if splits is not None and primary_value is None:
            primary_value = sum(s['value'] for s in splits)
            other_identifiers = []
            split_values = []

        if not split_request_id:
            split_request_id = str(int(time.time() * 1000))[-15:]

        # Generate auth token per Airpay docs:
        # PRIVATE_KEY = SHA256(APIKEY + '@' + USERNAME + ':|:' + PASSWORD)
        # TOKEN = HMAC-SHA256(PRIVATE_KEY + TRANSACTION_ID, APIKEY)
        private_key = hashlib.sha256(f"{api_key}@{self.username}:|:{self.password}".encode()).hexdigest()
        token = hmac.new(
            key=api_key.encode(),
            msg=f"{private_key}{airpay_id}".encode(),
            digestmod=hashlib.sha256
        ).hexdigest()

        payload = {
            'TRANSACTION_ID': airpay_id,
            'RRN': rrn,
            'SPLIT_REQUEST_ID': split_request_id,
            'PRIACCT_IDENTIFIER': primary_account,
            'PRIACCT_SPLIT_VALUE': str(primary_value),
            'TOKEN': token,
        }

        # Add other recipients only if present
        if other_identifiers:
            payload['OTHER_IDENTIFIERS'] = '|'.join(str(i) for i in other_identifiers)
            payload['SPLIT_VALUES'] = '|'.join(str(v) for v in split_values)

        headers = {
            'Content-Type': 'application/json',
            'Api-Key': api_key,
        }

        endpoint = "https://kraken.airpay.tz/airpay/neft/split_payout/transaction_level_split_config_api.php"

        try:
            response = requests.post(endpoint, json=payload, headers=headers, timeout=15)
            result = response.json() if response.content else {}
            success = result.get('errorCode') == '000'

            if success:
                logger.info(f"Split settlement successful for transaction {airpay_id}")
            else:
                error = result.get('errorCode', 'UNKNOWN')
                message = result.get('message', 'Unknown error')
                logger.error(f"Split settlement failed for {airpay_id}: [{error}] {message}")

            return {
                'success': success,
                'error': result.get('message') if not success else None,
                'error_code': result.get('errorCode'),
                'raw_response': result,
            }
        except Exception as e:
            logger.error(f"Split settlement request failed for {airpay_id}: {e}")
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
            }, timeout=15)
            
            if response.status_code == 200:
                try:
                    res_json = response.json()
                except json.JSONDecodeError:
                    text = response.text.strip()
                    last_brace = text.rfind('}')
                    res_json = json.loads(text[:last_brace+1])
                if 'response' in res_json:
                    decrypted = self.transformer.decrypt_string(res_json['response'], self.username, self.password)
                    try:
                        res_data = self.transformer.safe_json_loads(decrypted)
                    except Exception as e:
                        logger.error(f"AIRPAY_DEBUG: OAuth parse failed: {str(e)}. Decrypted: {decrypted}")
                        raise
                    
                    # Status check can be 'success' or code '200'
                    if res_data.get('status') == 'success' or res_data.get('status_code') == '200':
                        data = res_data.get('data', res_data)
                        token = data.get('access_token')
                        if token:
                            expires_in = int(data.get('expires_in', 300))
                            cache.set(cache_key, token, max(expires_in - 30, 60))
                            return token
                    if not token:
                        logger.error(f"AIRPAY_DEBUG: Token missing in decrypted response: {res_data}")
            
            logger.error(f"AIRPAY_DEBUG: OAuth Auth Failed. Status: {response.status_code}, Body: {response.text}")
            return ""
        except Exception as e:
            logger.error(f"Airpay Token Error: {str(e)}")
            return ""
