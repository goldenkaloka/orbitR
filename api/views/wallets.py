import logging
from decimal import Decimal, InvalidOperation

from api.serializers.wallets import TransactionSerializer, WalletSerializer
from apps.wallets.models import Wallet, WalletPayout, WalletTransaction
from django.utils import timezone
from domain.services.wallet_payout_services import (
    CASHOUT_ROLES,
    INSTANT_FEE_PERCENT,
    MAXIMUM_CASHOUT,
    MINIMUM_CASHOUT,
    CashoutError,
    CashoutService,
    cashout_wallets,
    driver_auto_payout_config,
    wallet_summary,
)
from domain.services.wallet_services import InsufficientFunds
from domain.utils import is_valid_tz_phone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

logger = logging.getLogger(__name__)

PROVIDERS = {
    'M_PESA': 'M-Pesa',
    'TIGO_PESA': 'Tigo Pesa',
    'AIRTEL_MONEY': 'Airtel Money',
    'HALOPESA': 'Halopesa',
    'MOBILE_MONEY': 'Mobile Money',
}


def _provider_name(key):
    return PROVIDERS.get(str(key or 'M_PESA').upper().replace(' ', '_'), 'Mobile Money')


def _fee_percent():
    return float((INSTANT_FEE_PERCENT * 100).normalize())


class WalletViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WalletSerializer

    #: Cashout moves real money — tighter budget than the blanket `user` rate.
    throttle_scope_map = {
        'cashout': 'payments',
    }

    def get_throttles(self):
        scope = self.throttle_scope_map.get(self.action)
        if scope:
            self.throttle_scope = scope
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get_queryset(self):
        return Wallet.objects.filter(
            id__in=[w.id for w in cashout_wallets(self.request.user)]
        ).prefetch_related('transactions')

    @action(detail=False, methods=['GET'])
    def me(self, request):
        wallets, balance, pending = wallet_summary(request.user)
        primary = wallets[0]

        txs = (
            WalletTransaction.objects
            .filter(wallet__in=wallets)
            .select_related('wallet', 'wallet__section')
            .order_by('-created_at')[:20]
        )
        role = getattr(request.user, 'serialized_role', getattr(request.user, 'role', 'customer'))
        can_cashout = role in CASHOUT_ROLES

        auto_payout = driver_auto_payout_config() if role == 'driver' else {
            'enabled': False, 'threshold': 0.0, 'time': '',
        }

        return Response({
            'id': str(primary.id),
            'balance': f"{balance:.2f}",
            'pending_balance': f"{pending:.2f}",
            'payout_available': f"{balance:.2f}" if can_cashout else '0.00',
            'can_cashout': can_cashout,
            'topup_enabled': False,
            'fee_percent': _fee_percent(),
            'minimum_cashout': float(MINIMUM_CASHOUT),
            'maximum_cashout': float(MAXIMUM_CASHOUT),
            'auto_payout_enabled': auto_payout['enabled'],
            'auto_payout_threshold': auto_payout['threshold'],
            'auto_payout_time': auto_payout['time'],
            'updated_at': max((w.updated_at for w in wallets), default=timezone.now()),
            'recent_transactions': TransactionSerializer(txs, many=True).data,
        })

    @action(detail=False, methods=['GET'])
    def transactions(self, request):
        """Merged, paginated history across every wallet the user owns."""
        wallets, _, _ = wallet_summary(request.user)
        try:
            limit = min(int(request.query_params.get('limit', 50)), 200)
            offset = max(int(request.query_params.get('offset', 0)), 0)
        except (TypeError, ValueError):
            limit, offset = 50, 0

        qs = (
            WalletTransaction.objects
            .filter(wallet__in=wallets)
            .select_related('wallet', 'wallet__section')
            .order_by('-created_at')
        )
        return Response({
            'count': qs.count(),
            'results': TransactionSerializer(qs[offset:offset + limit], many=True).data,
        })

    @action(detail=False, methods=['POST'])
    def calculate_fees(self, request):
        """Calculate fees for a given cashout amount."""
        try:
            amount_str = request.data.get('amount')
            if amount_str is None:
                return Response({"error": "Amount is required", "code": "MISSING_AMOUNT"}, status=400)

            amount = Decimal(str(amount_str)).quantize(Decimal('1'))
        except (InvalidOperation, TypeError, ValueError):
            return Response({"error": "Invalid amount format", "code": "INVALID_AMOUNT"}, status=400)

        if amount <= 0:
            return Response({"error": "Amount must be greater than zero", "code": "AMOUNT_TOO_LOW"}, status=400)

        if amount > MAXIMUM_CASHOUT:
            return Response({"error": "Amount exceeds maximum allowed", "code": "AMOUNT_TOO_HIGH"}, status=400)

        provider_key = request.data.get('provider', 'M_PESA')
        provider_name = _provider_name(provider_key)
        _, available, _ = wallet_summary(request.user)

        instant_fee = (amount * INSTANT_FEE_PERCENT).quantize(Decimal('1'))
        total_fees = instant_fee
        net_amount = amount - total_fees

        return Response({
            "cashout_amount": float(amount),
            "instant_fee": float(instant_fee),
            "total_fees": float(total_fees),
            "net_amount": float(net_amount),
            # The client must display the fee it is actually charged. The old
            # UI hard-coded 1% while the server took 0.5%, and read a
            # `provider_fee` key that never existed.
            "fee_percent": _fee_percent(),
            "provider_fee": 0.0,
            "provider_name": provider_name,
            "available_balance": float(available),
            "minimum_cashout": float(MINIMUM_CASHOUT),
        })

    @action(detail=False, methods=['POST'])
    def cashout(self, request):
        logger.info(f"Cashout requested by user {request.user.id}")

        phone = request.data.get('phone', '')
        if not phone:
            return Response({"error": "Phone number is required", "code": "MISSING_PHONE"}, status=400)
        if not is_valid_tz_phone(phone):
            return Response({
                "error": "Invalid Tanzanian phone number. Use format: 0712 345 678",
                "code": "INVALID_PHONE",
            }, status=400)

        provider_key = request.data.get('provider', 'M_PESA')
        provider_name = _provider_name(provider_key)

        amount = request.data.get('amount')
        if amount is not None:
            try:
                amount = Decimal(str(amount))
            except (InvalidOperation, TypeError, ValueError):
                return Response({"error": "Invalid amount format", "code": "INVALID_AMOUNT"}, status=400)

        idempotency_key = (
            request.headers.get('Idempotency-Key')
            or request.data.get('idempotency_key')
            or None
        )

        try:
            payout = CashoutService.execute(
                user=request.user,
                amount=amount,
                phone=phone,
                provider=provider_name,
                idempotency_key=str(idempotency_key)[:128] if idempotency_key else None,
            )
        except CashoutError as exc:
            payload = {"error": exc.message, "code": exc.code}
            payload.update(exc.extra)
            return Response(payload, status=exc.status_code)
        except InsufficientFunds as exc:
            return Response(
                {"error": str(exc), "code": "INSUFFICIENT_BALANCE"},
                status=400,
            )
        except Exception:
            logger.exception(f"Cashout failed for user {request.user.id}")
            return Response(
                {"error": "Cashout failed. Please try again.", "code": "CASHOUT_FAILED"},
                status=500,
            )

        body = {
            "payout_id": str(payout.id),
            "status": payout.status,
            "cashout_amount": float(payout.amount),
            "instant_fee": float(payout.fee),
            "total_fees": float(payout.fee),
            "net_amount": float(payout.net_amount),
            "fee_percent": _fee_percent(),
            "provider_name": payout.provider,
            "provider_reference": payout.provider_reference,
        }

        if payout.status == 'SUCCESS':
            body["message"] = "Cashout successful"
            return Response(body)

        if payout.status == 'UNKNOWN':
            body["message"] = (
                "Cashout is being confirmed with the mobile money provider. "
                "Your wallet will be credited automatically if it fails."
            )
            return Response(body, status=202)

        if payout.status in ('PROCESSING', 'PENDING'):
            body["message"] = "Cashout is being processed."
            return Response(body, status=202)

        body["message"] = "Cashout failed. Your wallet has been credited back."
        body["error"] = payout.failure_reason or "Disbursement failed"
        return Response(body, status=400)

    @action(detail=False, methods=['GET'])
    def payouts(self, request):
        qs = WalletPayout.objects.filter(actor=request.user).order_by('-created_at')[:50]
        return Response([
            {
                'id': str(p.id),
                'status': p.status,
                'amount': float(p.amount),
                'fee': float(p.fee),
                'net_amount': float(p.net_amount),
                'provider': p.provider,
                'destination_phone': p.destination_phone,
                'provider_reference': p.provider_reference,
                'failure_reason': p.failure_reason,
                # The nightly driver sweep keys its idempotency row on
                # ``auto-payout:<driver>:<day>``, so this is the only way the
                # app can tell an automatic transfer from a manual cashout.
                'is_automatic': bool(
                    p.idempotency_key and p.idempotency_key.startswith('auto-payout:')
                ),
                'created_at': p.created_at,
                'completed_at': p.completed_at,
            }
            for p in qs
        ])

    @action(detail=False, methods=['GET'], url_path='stalls')
    def list_stall_wallets(self, request):
        """Restaurant owner (super admin) lists wallets and transactions of all their stalls."""
        if request.user.role != 'vendor':
            return Response({'error': 'Only restaurant owners can view stall wallets'}, status=403)

        from apps.restaurants.models import Restaurant, RestaurantSection
        try:
            restaurant = Restaurant.objects.get(owner=request.user)
        except Restaurant.DoesNotExist:
            return Response({'error': 'You do not own a restaurant'}, status=404)

        sections = RestaurantSection.objects.filter(restaurant=restaurant)
        wallets = Wallet.objects.filter(section__in=sections).select_related('section').prefetch_related('transactions')

        data = []
        for wallet in wallets:
            data.append({
                'section_id': str(wallet.section.id),
                'section_name': wallet.section.name,
                'balance': float(wallet.balance),
                'pending_balance': float(wallet.pending_balance),
                'transactions': TransactionSerializer(wallet.transactions.all()[:10], many=True).data
            })
        return Response(data)
