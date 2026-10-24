import hashlib
import hmac
import json
import logging
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

from .base import PaymentGateway

logger = logging.getLogger(__name__)


class ClickpesaGateway(PaymentGateway):
    """
    ClickPesa Payment Gateway Implementation.
    """
    def __init__(self):
        self.config = getattr(settings, 'CLICKPESA_CONFIG', {})
        self.client_id = self.config.get('CLIENT_ID', '')
        self.api_key = self.config.get('API_KEY', '')
        self.secret_key = self.config.get('SECRET_KEY', '')
        self.base_url = self.config.get('BASE_URL', 'https://api.clickpesa.com').rstrip('/')

    def _get_token(self) -> str:
        """
        Gets a valid JWT token. Checks the Django cache first.
        """
        cache_key = f"clickpesa_token_{self.client_id}"
        token = cache.get(cache_key)
        if token:
            return token

        # Back off after a failure: an invalid/rotated API key would otherwise
        # be retried on every order and trip ClickPesa's token rate limit.
        failure_key = f"{cache_key}_failed"
        if cache.get(failure_key):
            return ""

        try:
            url = f"{self.base_url}/third-parties/generate-token"
            headers = {
                "api-key": self.api_key,
                "client-id": self.client_id,
            }
            logger.info("Requesting new ClickPesa JWT access token")
            # ClickPesa expects a bodyless POST here; sending a JSON body makes
            # the gateway reject the request as "Unauthorized API-Key".
            response = requests.post(url, headers=headers, timeout=15)
            if response.status_code == 200:
                data = response.json()
                token = data.get('token')
                # Cache token. ClickPesa JWT token usually expires in 1 hour.
                # Cache for 50 minutes (3000 seconds) to be safe.
                if token:
                    cache.set(cache_key, token, 3000)
                    return token
            logger.error(f"Failed to generate ClickPesa token. HTTP {response.status_code}: {response.text}")
        except Exception as e:
            logger.error(f"ClickPesa token generation exception: {str(e)}")

        cache.set(failure_key, 1, 180)
        return ""

    def _generate_checksum(self, payload: dict) -> str:
        """
        Generates HMAC-SHA256 checksum by sorting keys alphabetically
        and stringifying the payload compactly.
        """
        # 1. Alphabetically sort keys recursively
        sorted_payload = self._sort_dict_keys(payload)
        # 2. Convert sorted object to compact JSON string
        json_str = json.dumps(sorted_payload, separators=(',', ':'))
        # 3. Create HMAC-SHA256 hash using the secret key
        hash_val = hmac.new(self.secret_key.encode(), json_str.encode(), hashlib.sha256).hexdigest()
        return hash_val

    def _sort_dict_keys(self, d: Any) -> Any:
        if isinstance(d, dict):
            return {k: self._sort_dict_keys(v) for k, v in sorted(d.items())}
        elif isinstance(d, list):
            return [self._sort_dict_keys(x) for x in d]
        else:
            return d

    def collect(self, amount, phone, order_reference, **kwargs):
        """
        Initiates mobile money payment collection (USSD Push).
        """
        try:
            token = self._get_token()
            if not token:
                return {'success': False, 'error': 'Failed to obtain ClickPesa token'}

            url = f"{self.base_url}/third-parties/payments/initiate-ussd-push-request"
            headers = {
                "Authorization": token,
                "Content-Type": "application/json"
            }

            # Normalize Tanzanian phone number format: expects international format like '255712345678'
            formatted_phone = str(phone).replace('+', '').replace(' ', '')
            if formatted_phone.startswith('0'):
                formatted_phone = '255' + formatted_phone[1:]

            # ClickPesa requires alphanumeric order references with no spaces/symbols (max 20 characters)
            # We use 16 chars of UUID and append a random 3-char suffix to prevent duplicate conflicts (409) on retries
            import re
            import random
            import string
            random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=3))
            sanitized_ref = re.sub(r'[^a-zA-Z0-9]', '', str(order_reference))[:16] + random_suffix

            payload = {
                "amount": str(int(float(amount))),
                "currency": "TZS",
                "phoneNumber": formatted_phone,
                "orderReference": sanitized_ref,
            }

            checksum = self._generate_checksum(payload)
            payload["checksum"] = checksum

            logger.info(f"Initiating ClickPesa USSD Push Collection for order {sanitized_ref}")
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            if response.status_code in [200, 201]:
                res_data = response.json()
                return {
                    'success': True,
                    'provider_reference': res_data.get('transactionId') or res_data.get('id'),
                    'raw_data': res_data
                }
            else:
                logger.error(f"ClickPesa Collection failed: HTTP {response.status_code}: {response.text}")
                try:
                    res_json = response.json()
                    error_msg = res_json.get('message') or f"ClickPesa API Error: {response.status_code}"
                except Exception:
                    error_msg = f"ClickPesa API Error: {response.status_code}"
                return {'success': False, 'error': error_msg}
        except Exception as e:
            logger.error(f"ClickPesa collection exception: {str(e)}")
            return {'success': False, 'error': str(e)}

    def disburse(self, amount, account_info, reference, **kwargs):
        """
        Disburse funds to a driver or restaurant wallet (manual cashout).
        """
        try:
            token = self._get_token()
            if not token:
                return {'success': False, 'error': 'Failed to obtain ClickPesa token'}

            url = f"{self.base_url}/third-parties/payouts/create-mobile-money-payout"
            headers = {
                "Authorization": token,
                "Content-Type": "application/json"
            }

            # Normalize Tanzanian phone number format
            formatted_phone = str(account_info).replace('+', '').replace(' ', '')
            if formatted_phone.startswith('0'):
                formatted_phone = '255' + formatted_phone[1:]

            # ClickPesa requires alphanumeric order references (max 20 characters)
            # We use 16 chars of UUID and append a random 3-char suffix to prevent duplicate conflicts (409) on retries
            import re
            import random
            import string
            random_suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=3))
            sanitized_ref = re.sub(r'[^a-zA-Z0-9]', '', str(reference))[:16] + random_suffix

            payload = {
                "amount": int(float(amount)),
                "currency": "TZS",
                "phoneNumber": formatted_phone,
                "orderReference": sanitized_ref,
            }

            checksum = self._generate_checksum(payload)
            payload["checksum"] = checksum

            logger.info(f"Initiating ClickPesa manual payout for cashout {sanitized_ref}")
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=30)
            except requests.exceptions.RequestException as exc:
                # A timeout or dropped connection is NOT a rejection: the
                # gateway may have already paid out. Returning a plain failure
                # would make the caller refund the wallet and pay twice.
                logger.error(f"ClickPesa payout transport error: {exc}")
                return {
                    'success': False,
                    'unknown': True,
                    'error': f'Payout outcome unknown: {exc}',
                }

            if response.status_code in [200, 201]:
                res_data = response.json()
                return {
                    'success': True,
                    'provider_reference': res_data.get('transactionId') or res_data.get('id'),
                    'message': 'Disbursement successful',
                    'raw_data': res_data
                }

            unknown = response.status_code >= 500 or response.status_code == 409
            logger.error(f"ClickPesa Payout failed: HTTP {response.status_code}: {response.text}")
            try:
                res_json = response.json()
                error_msg = res_json.get('message') or f"ClickPesa Payout API Error: {response.status_code}"
            except Exception:
                error_msg = f"ClickPesa Payout API Error: {response.status_code}"
            return {'success': False, 'unknown': unknown, 'error': error_msg}
        except Exception as e:
            logger.error(f"ClickPesa payout exception: {str(e)}")
            return {'success': False, 'unknown': True, 'error': str(e)}

    def get_status(self, provider_reference):
        """
        Query status of a transaction.
        """
        try:
            token = self._get_token()
            if not token:
                return {'success': False, 'error': 'Failed to obtain ClickPesa token'}

            url = f"{self.base_url}/third-parties/payments/status"
            headers = {
                "Authorization": token,
                "Content-Type": "application/json"
            }

            payload = {
                "transactionId": str(provider_reference)
            }

            checksum = self._generate_checksum(payload)
            payload["checksum"] = checksum

            response = requests.post(url, headers=headers, json=payload, timeout=20)
            if response.status_code == 200:
                res_data = response.json()
                status_str = str(res_data.get('status') or '').upper()
                return {
                    'success': True,
                    'status': 'SUCCESS' if status_str == 'SUCCESS' else 'FAILED' if status_str == 'FAILED' else 'PENDING',
                    'raw_status': status_str,
                    'data': res_data
                }
            return {'success': False, 'error': f"ClickPesa API Error: {response.status_code}"}
        except Exception as e:
            logger.error(f"ClickPesa status exception: {str(e)}")
            return {'success': False, 'error': str(e)}

    def refund(self, transaction_id, amount, **kwargs):
        """
        Refunds a payment back to the customer.
        """
        try:
            token = self._get_token()
            if not token:
                return {'success': False, 'error': 'Failed to obtain ClickPesa token'}

            url = f"{self.base_url}/third-parties/payments/refund"
            headers = {
                "Authorization": token,
                "Content-Type": "application/json"
            }

            payload = {
                "transactionId": str(transaction_id),
                "amount": int(float(amount))
            }

            checksum = self._generate_checksum(payload)
            payload["checksum"] = checksum

            response = requests.post(url, headers=headers, json=payload, timeout=30)
            if response.status_code == 200:
                res_data = response.json()
                return {'success': True, 'refund_id': res_data.get('refundId') or res_data.get('id')}
            else:
                logger.error(f"ClickPesa Refund failed: HTTP {response.status_code}: {response.text}")
                try:
                    res_json = response.json()
                    error_msg = res_json.get('message') or f"ClickPesa Refund API Error: {response.status_code}"
                except Exception:
                    error_msg = f"ClickPesa Refund API Error: {response.status_code}"
                return {'success': False, 'error': error_msg}
        except Exception as e:
            logger.error(f"ClickPesa refund exception: {str(e)}")
            return {'success': False, 'error': str(e)}
