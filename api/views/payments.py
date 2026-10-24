import hashlib
import hmac
import json
import logging
import os

from django.conf import settings
from domain.services.payment_services import CompletePaymentService
from infrastructure.payment_gateways.airpay import AirpayGateway
from infrastructure.payment_gateways.airpay_utils import AirpayTransformer
from rest_framework import permissions, status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

# Airpay known server IPs (update as provided by Airpay)
AIRPAY_ALLOWED_IPS = (os.getenv('AIRPAY_ALLOWED_IPS') or '41.59.0.0/16').split(',')

class InitiatePaymentView(APIView):
    """
    Unified entry point for all payments (Order, Pool Share, Marketplace Listing).
    """
    permission_classes = [permissions.IsAuthenticated]

    #: Money-moving writes get a tighter budget than the blanket `user` rate
    #: so a compromised or buggy client cannot hammer the payment gateway.
    def get_throttles(self):
        self.throttle_scope = 'payments'
        return [ScopedRateThrottle()]

    def post(self, request, *args, **kwargs):
        payable_type = request.data.get('payable_type')
        payable_id = request.data.get('payable_id')
        phone = request.data.get('phone', request.user.phone)
        bank_code = request.data.get('bank_code')
        method = (request.data.get('method') or request.data.get('payment_method') or 'MOBILE_MONEY').upper()

        if not payable_type or not payable_id:
            return Response({'error': 'payable_type and payable_id are required'}, status=400)

        if method not in ('MOBILE_MONEY', 'WALLET'):
            return Response({'error': 'method must be MOBILE_MONEY or WALLET'}, status=400)

        try:
            from domain.services.payment_services import InitiatePaymentService
            result = InitiatePaymentService.execute(
                payable_type=payable_type,
                payable_id=payable_id,
                phone=phone,
                bank_code=bank_code,
                actor=request.user,
                method=method,
            )
            
            payment = result['payment']
            gateway_data = result.get('gateway_response', {})

            push_sent = gateway_data.get('success', False)

            base_response = {
                "success": push_sent,
                "payment_id": str(payment.id),
                "status": payment.status,
                "method": payment.method,
                "is_push": result.get('is_push', False),
            }

            if push_sent:
                base_response["provider_reference"] = gateway_data.get('provider_reference')
                base_response["message"] = gateway_data.get('message')
                base_response["instant_success"] = gateway_data.get('instant_success')
                if gateway_data.get('params'):
                    base_response["params"] = gateway_data['params']
                if gateway_data.get('endpoint'):
                    base_response["endpoint"] = gateway_data['endpoint']
            else:
                base_response["error"] = gateway_data.get('error') or 'Payment initiation failed'
                base_response["code"] = gateway_data.get('code')

            return Response(base_response)
        except ValueError as exc:
            logger.warning(f"Payment initiation rejected: {exc}")
            return Response({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Payment initiation failed")
            return Response({"error": "Payment initiation failed."}, status=400)

class AirpayWebhookView(APIView):
    """
    Airpay Tanzania Webhook Handler (v4 Official Logic).
    Secured with IP allowlist and shared-secret signature verification.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    # Gateway callbacks are authenticated by IP allowlist + signature, not by
    # DRF throttling — the default anon bucket is keyed by the gateway IP and
    # would 429 legitimate callbacks after 100/hour (DDIA Ch.1 backpressure).
    throttle_classes = []

    def _get_client_ip(self, request):
        """Extract client IP. Trust proxy headers only when explicitly enabled."""
        if getattr(settings, 'TRUST_PROXY_HEADERS', False):
            x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
        else:
            x_forwarded = None
        if x_forwarded:
            return x_forwarded.split(',')[0].strip()
        return request.META.get('REMOTE_ADDR')

    def _verify_signature(self, request, gateway):
        """
        Verify webhook authenticity using HMAC-SHA256 of the raw body 
        signed with the merchant secret key.
        Airpay IPN callbacks may not send this header; allowing unsigned callbacks
        must be an explicit deployment setting and still requires checksum validation.
        """
        signature = request.META.get('HTTP_X_AIRPAY_SIGNATURE', '')
        if not signature:
            allow_unsigned = getattr(settings, 'AIRPAY_ALLOW_UNSIGNED_WEBHOOKS', False)
            if allow_unsigned:
                logger.warning(
                    f"Airpay Webhook: X-AIRPAY-SIGNATURE header absent from {self._get_client_ip(request)}; "
                    f"falling back to IP allowlist plus Airpay checksum."
                )
            return allow_unsigned
        
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

    def _extract_callback_data(self, request, gateway):
        """
        Extract and verify callback data from Airpay.
        Supports two formats:
          Format 1 — Encrypted:  {'response': '<encrypted_string>'}
          Format 2 — IPN Direct: JSON body with direct fields + ap_SecureHash
        Returns (data_dict, ap_transactionid, transaction_status, orderid) or raises.
        """
        response_data = request.data.get('response')
        if response_data:
            decrypted_json = AirpayTransformer.decrypt_string(
                response_data,
                gateway.username,
                gateway.password
            )
            data_dict = json.loads(decrypted_json)
            actual_data = data_dict.get('data', data_dict)
        else:
            actual_data = request.data.dict() if hasattr(request.data, 'dict') else dict(request.data)

        if not AirpayTransformer.verify_checksum_v4(actual_data, gateway.mercid, gateway.username):
            raise ValueError("CRC32 checksum verification failed")

        return (
            actual_data,
            actual_data.get('ap_transactionid'),
            actual_data.get('transaction_status'),
            actual_data.get('orderid'),
        )

    def post(self, request, *args, **kwargs):
        client_ip = self._get_client_ip(request)

        # Security Gate 0: Verify IP Allowlist
        if not self._is_ip_allowed(client_ip):
            logger.warning(f"Airpay Webhook: Unauthorized access attempt from IP {client_ip}")
            return Response({"status": "error", "message": "Forbidden"}, status=403)

        gateway = AirpayGateway()

        # Security Gate 1: Verify request signature (optional — absent in IPN callbacks)
        if not self._verify_signature(request, gateway):
            logger.warning(f"Airpay Webhook: Signature verification failed from IP {client_ip}")
            return Response({"status": "error", "message": "Invalid signature"}, status=400)

        try:
            actual_data, ap_transaction_id, transaction_status, order_id = self._extract_callback_data(request, gateway)

            logger.info(
                f"Airpay Webhook from {client_ip}: "
                f"order={order_id} ap_txn={ap_transaction_id} status={transaction_status}"
            )

            success = str(transaction_status) == '200'

            CompletePaymentService.execute(
                provider_reference=ap_transaction_id,
                success=success,
                reference_id=order_id,
            )

            return Response({"status": "success"}, status=200)

        except Exception:
            logger.exception("Error processing Airpay webhook")
            return Response({"error": "Webhook processing failed."}, status=500)


class ClickpesaWebhookView(APIView):
    """
    ClickPesa Webhook Callback Handler.
    """
    permission_classes = [AllowAny]
    authentication_classes = []
    # Authenticated by checksum signature, not by DRF throttling — the anon
    # bucket is keyed by the gateway IP and would 429 legitimate callbacks.
    throttle_classes = []

    def _verify_checksum(self, request):
        try:
            config = getattr(settings, 'CLICKPESA_CONFIG', {})
            secret_key = config.get('SECRET_KEY', '')
            if not secret_key:
                logger.error("ClickPesa SECRET_KEY not configured. Webhook skipped.")
                return False

            payload_data = request.data.get('data')
            if not isinstance(payload_data, dict):
                logger.warning("ClickPesa Webhook: Missing or invalid 'data' attribute in payload.")
                return False

            data = dict(payload_data)
            checksum_in_body = data.pop('checksum', None)
            data.pop('checksumMethod', None)

            # Retrieve signature from headers (check mixed-case, lowercase, or payload body fallback)
            checksum = (
                request.headers.get('X-ClickPesa-Signature')
                or request.headers.get('x-clickpesa-signature')
                or checksum_in_body
            )

            if not checksum:
                logger.warning("ClickPesa Webhook: Missing checksum in headers or request payload.")
                return False

            def sort_dict_keys(d):
                if isinstance(d, dict):
                    return {k: sort_dict_keys(v) for k, v in sorted(d.items())}
                elif isinstance(d, list):
                    return [sort_dict_keys(x) for x in d]
                else:
                    return d

            sorted_data = sort_dict_keys(data)
            json_str = json.dumps(sorted_data, separators=(',', ':'))
            expected_checksum = hmac.new(secret_key.encode(), json_str.encode(), hashlib.sha256).hexdigest()

            return checksum and hmac.compare_digest(checksum, expected_checksum)
        except Exception as e:
            logger.error(f"Error validating ClickPesa webhook checksum: {str(e)}")
            return False

    def post(self, request, *args, **kwargs):
        logger.warning(f"Received ClickPesa webhook data: {request.data}")
        logger.warning(f"ClickPesa webhook headers: {dict(request.headers)}")

        if not self._verify_checksum(request):
            logger.warning("ClickPesa Webhook: Checksum validation failed.")
            return Response({"status": "error", "message": "Invalid signature"}, status=400)

        try:
            data = request.data
            event_data = data.get('data') or {}
            transaction_id = event_data.get('id') or event_data.get('transactionId')
            transaction_status = str(event_data.get('status') or '').upper()
            order_ref = event_data.get('orderReference')

            if not transaction_id or not order_ref:
                return Response({"status": "error", "message": "Missing transactionId or orderReference"}, status=400)

            logger.info(
                f"Processing ClickPesa Webhook: "
                f"order={order_ref} txn={transaction_id} status={transaction_status}"
            )

            # Strip the 3-character random suffix to get the 16-character UUID prefix for lookup
            lookup_ref = order_ref[:16] if order_ref else ''

            from apps.payments.models import Payment

            def payment_by_id_prefix(prefix, **extra_filters):
                """Resolve id.hex[:16] as a UUID range (prefix+000.. to prefix+fff..)
                so Postgres answers with a PK index range scan — the old Python
                loops materialised every INITIATED payment (and every payment
                from the last hour) just to compare hex prefixes in memory
                (DDIA Ch.1: bounded work per webhook)."""
                if not prefix or len(prefix) > 32 or '-' in prefix:
                    return None
                try:
                    from uuid import UUID
                    low = UUID(prefix + '0' * (32 - len(prefix)))
                    high = UUID(prefix + 'f' * (32 - len(prefix)))
                except ValueError:
                    return None
                return (
                    Payment.objects
                    .filter(id__gte=low, id__lte=high, **extra_filters)
                    .order_by('-created_at')
                    .first()
                )

            payment = Payment.objects.filter(provider_reference=transaction_id).first()

            if not payment:
                payment = payment_by_id_prefix(lookup_ref, status='INITIATED')

            if not payment:
                from django.utils import timezone
                from datetime import timedelta
                time_threshold = timezone.now() - timedelta(hours=1)
                payment = payment_by_id_prefix(lookup_ref, created_at__gte=time_threshold)

            if not payment:
                logger.error(f"ClickPesa Webhook: Could not resolve payment for reference {order_ref}")
                return Response({"status": "error", "message": "Payment record not found"}, status=404)

            if not payment.provider_reference:
                payment.provider_reference = transaction_id
                payment.save()

            success = transaction_status in ('SUCCESS', 'SUCCESSFUL', 'COMPLETED', 'PAID')
            terminal_failure = transaction_status in (
                'FAILED', 'FAILURE', 'CANCELLED', 'CANCELED',
                'REJECTED', 'DECLINED', 'EXPIRED', 'TIMEOUT',
            )

            # PENDING / PROCESSING / INPROCESS etc. are not final. Do not pass
            # them through as a failure (that would mark a still-in-flight
            # collection FAILED); acknowledge and let the cleanup poller or a
            # later callback decide.
            if not success and not terminal_failure:
                logger.info(
                    f"ClickPesa webhook: non-terminal status '{transaction_status}' "
                    f"for payment {payment.id}; awaiting final callback"
                )
                return Response({"status": "pending"}, status=200)

            CompletePaymentService.execute(
                payment_id=payment.id,
                provider_reference=transaction_id,
                success=success,
                reference_id=order_ref,
            )

            return Response({"status": "success"}, status=200)
        except Exception:
            logger.exception("Error processing ClickPesa webhook callback")
            return Response({"error": "Webhook processing failed."}, status=500)


class PlatformRevenueView(APIView):
    """Platform revenue analytics from Transaction records."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request, *args, **kwargs):
        from apps.payments.models import Transaction
        from django.db.models import Sum
        from django.utils import timezone
        from datetime import timedelta

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = today_start.replace(day=1)

        base_qs = Transaction.objects.filter(
            transaction_type='platform_revenue',
            status='COMPLETED',
        )

        total_revenue = base_qs.aggregate(Sum('amount'))['amount__sum'] or 0
        revenue_today = base_qs.filter(created_at__gte=today_start).aggregate(Sum('amount'))['amount__sum'] or 0
        revenue_month = base_qs.filter(created_at__gte=month_start).aggregate(Sum('amount'))['amount__sum'] or 0

        last_30 = now - timedelta(days=30)
        daily = (
            base_qs.filter(created_at__gte=last_30)
            .extra({'day': "date(created_at)"})
            .values('day')
            .annotate(amount=Sum('amount'))
            .order_by('day')
        )

        return Response({
            'total_revenue': float(total_revenue),
            'revenue_today': float(revenue_today),
            'revenue_this_month': float(revenue_month),
            'daily_revenue': [
                {'date': str(entry['day']), 'amount': float(entry['amount'])}
                for entry in daily
            ],
        })
