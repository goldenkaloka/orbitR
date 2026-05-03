from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from domain.services.payment_services import CompletePaymentService
from infrastructure.payment_gateways.airpay import AirpayGateway
from infrastructure.payment_gateways.airpay_utils import AirpayTransformer
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

class AirpayWebhookView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        """
        Airpay Callback Handler
        Decrypts the response and completes the payment.
        """
        encrypted_response = request.data.get('response')
        if not encrypted_response:
            return Response({"error": "Missing response"}, status=400)

        try:
            # Decrypt the Airpay payload
            data = AirpayTransformer.decrypt(encrypted_response, settings.AIRPAY_CONFIG['SECRET_KEY'])
            logger.info(f"Airpay Webhook Decrypted: {data}")

            # Verify Secure Hash (CRC32)
            received_hash = data.get('ap_SecureHash')
            if not received_hash:
                logger.warning("Airpay Webhook received without ap_SecureHash field.")
                return Response({"error": "Missing Secure Hash"}, status=401)
                
            is_valid = AirpayTransformer.verify_secure_hash(
                received_hash, 
                data, 
                settings.AIRPAY_CONFIG['USERNAME']
            )
            if not is_valid:
                logger.warning(f"Airpay SecureHash Mismatch! Received: {received_hash}")
                return Response({"error": "Invalid Secure Hash"}, status=401)

            order_id = data.get('orderid')
            ap_transactionid = data.get('ap_transactionid')
            rrn = data.get('rrn')
            status_desc = str(data.get('transaction_payment_status')).upper()

            if order_id or ap_transactionid:
                from apps.payments.models import Payment
                # Idempotency check at view level
                if ap_transactionid and Payment.objects.filter(provider_reference=ap_transactionid, status__in=['COLLECTED', 'RELEASE_READY', 'DISBURSED']).exists():
                    logger.info(f"Duplicate Airpay webhook for {ap_transactionid} ignored.")
                    return Response({"status": "success", "detail": "Already processed"}, status=200)

                success = status_desc == 'SUCCESS'
                CompletePaymentService.execute(
                    provider_reference=ap_transactionid,
                    success=success,
                    order_id=order_id,
                    rrn=rrn
                )
                return Response({"status": "success"}, status=200)

        except Exception as e:
            logger.error(f"Error processing Airpay webhook: {str(e)}")
            return Response({"error": str(e)}, status=500)

        return Response({"error": "Invalid Payload"}, status=400)

