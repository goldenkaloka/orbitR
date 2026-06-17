from rest_framework import viewsets, status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from domain.services.payment_services import CompletePaymentService
from infrastructure.payment_gateways.airpay import AirpayGateway
from infrastructure.payment_gateways.airpay_utils import AirpayTransformer
from django.conf import settings
import logging
import json
import os
import hashlib
import hmac

logger = logging.getLogger(__name__)

# Airpay known server IPs (update as provided by Airpay)
AIRPAY_ALLOWED_IPS = (os.getenv('AIRPAY_ALLOWED_IPS') or '41.59.0.0/16').split(',')

class InitiatePaymentView(APIView):
    """
    Unified entry point for all payments (Order, Pool Share, Marketplace Listing).
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        payable_type = request.data.get('payable_type')
        payable_id = request.data.get('payable_id')
        provider = request.data.get('provider', 'AIRPAY')
        phone = request.data.get('phone', request.user.phone)
        bank_code = request.data.get('bank_code')

        if not payable_type or not payable_id:
            return Response({'error': 'payable_type and payable_id are required'}, status=400)

        try:
            from domain.services.payment_services import InitiatePaymentService
            result = InitiatePaymentService.execute(
                payable_type=payable_type,
                payable_id=payable_id,
                provider=provider,
                phone=phone,
                bank_code=bank_code
            )
            
            payment = result['payment']
            gateway_data = result.get('gateway_response', {})

            push_sent = gateway_data.get('success', False)

            base_response = {
                "success": push_sent,
                "payment_id": str(payment.id),
                "status": payment.status,
                "is_push": result.get('is_push', False),
            }

            if push_sent:
                base_response["provider_reference"] = gateway_data.get('provider_reference')
                base_response["message"] = gateway_data.get('message')
                base_response["raw_data"] = gateway_data.get('raw_data')
                base_response["instant_success"] = gateway_data.get('instant_success')
                if gateway_data.get('params'):
                    base_response["params"] = gateway_data['params']
                if gateway_data.get('endpoint'):
                    base_response["endpoint"] = gateway_data['endpoint']
            else:
                base_response["error"] = gateway_data.get('error', 'Payment initiation failed')
                base_response["code"] = gateway_data.get('code')

            return Response(base_response)
        except Exception as e:
            return Response({"error": str(e)}, status=400)

class AirpayWebhookView(APIView):
    """
    Airpay Tanzania Webhook Handler (v4 Official Logic).
    Secured with IP allowlist and shared-secret signature verification.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def _get_client_ip(self, request):
        """Extract real client IP, respecting proxy headers."""
        x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded:
            return x_forwarded.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR')

    def _verify_signature(self, request, gateway):
        """
        Verify webhook authenticity using HMAC-SHA256 of the raw body 
        signed with the merchant secret key.
        """
        signature = request.META.get('HTTP_X_AIRPAY_SIGNATURE', '')
        if not signature:
            return False
        
        raw_body = request.body
        secret = gateway.secret or ''
        expected = hmac.new(
            secret.encode('utf-8'),
            raw_body,
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    def _is_ip_allowed(self, client_ip):
        """Check if the client IP is in the allowed list/range."""
        import ipaddress
        for allowed in AIRPAY_ALLOWED_IPS:
            try:
                if '/' in allowed:
                    if ipaddress.ip_address(client_ip) in ipaddress.ip_network(allowed):
                        return True
                elif client_ip == allowed:
                    return True
            except ValueError:
                continue
        return False

    def post(self, request, *args, **kwargs):
        client_ip = self._get_client_ip(request)
        
        # Security Gate 0: Verify IP Allowlist
        if not self._is_ip_allowed(client_ip):
            logger.warning(f"Airpay Webhook: Unauthorized access attempt from IP {client_ip}")
            return Response({"status": "error", "message": "Forbidden"}, status=403)

        gateway = AirpayGateway()

        # Security Gate 1: Verify request signature
        if not self._verify_signature(request, gateway):
            logger.warning(f"Airpay Webhook: Signature verification failed from IP {client_ip}")
            return Response({"status": "error", "message": "Invalid signature"}, status=400)

        # Airpay v4 usually sends a POST with a 'response' field containing encrypted data
        response_data = request.data.get('response')
        
        if not response_data:
            logger.error("Airpay Webhook: No 'response' field found in payload")
            return Response({"status": "error", "message": "Missing response"}, status=400)

        try:
            # 1. Decrypt and Verify
            decrypted_json = AirpayTransformer.decrypt_string(
                response_data, 
                gateway.username, 
                gateway.password
            )
            data_dict = json.loads(decrypted_json)
            actual_data = data_dict.get('data', {})
            
            logger.info(f"Airpay Webhook Decrypted from {client_ip}: order={actual_data.get('orderid')}")

            # 2. Check CRC32 Hash
            if not AirpayTransformer.verify_checksum_v4(actual_data, gateway.mercid, gateway.username):
                logger.error("Airpay Webhook: CRC32 Verification Failed")
                return Response({"status": "error", "message": "Invalid Secure Hash"}, status=400)

            # 3. Extract Order Details
            status = actual_data.get('transaction_status')
            order_id = actual_data.get('orderid')
            ap_transaction_id = actual_data.get('ap_transactionid')
            
            success = status == '200'

            CompletePaymentService.execute(
                provider_reference=ap_transaction_id,
                success=success,
                reference_id=order_id,
            )
            
            return Response({"status": "success"}, status=200)

        except Exception as e:
            logger.error(f"Error processing Airpay webhook: {str(e)}")
            return Response({"error": str(e)}, status=500)
