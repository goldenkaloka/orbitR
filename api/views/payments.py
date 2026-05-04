from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from domain.services.payment_services import CompletePaymentService
from infrastructure.payment_gateways.airpay import AirpayGateway
from infrastructure.payment_gateways.airpay_utils import AirpayTransformer
from django.conf import settings
import logging
import json

logger = logging.getLogger(__name__)

class AirpayWebhookView(APIView):
    """
    Airpay Tanzania Webhook Handler (v4 Official Logic).
    """
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        # Airpay v4 usually sends a POST with a 'response' field containing encrypted data
        response_data = request.data.get('response')
        
        if not response_data:
            logger.error("Airpay Webhook: No 'response' field found in payload")
            return Response({"status": "error", "message": "Missing response"}, status=400)

        gateway = AirpayGateway()
        
        try:
            # 1. Decrypt and Verify
            # The Gateway.verify method now handles decryption and CRC32 verification
            decrypted_json = AirpayTransformer.decrypt_string(
                response_data, 
                gateway.username, 
                gateway.password
            )
            data_dict = json.loads(decrypted_json)
            actual_data = data_dict.get('data', {})
            
            logger.info(f"Airpay Webhook Decrypted: {actual_data}")

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
                order_id=order_id,
            )
            
            return Response({"status": "success"}, status=200)

        except Exception as e:
            logger.error(f"Error processing Airpay webhook: {str(e)}")
            return Response({"error": str(e)}, status=500)


