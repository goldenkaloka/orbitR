import logging
from decimal import Decimal, InvalidOperation

from api.serializers.wallets import TransactionSerializer, WalletSerializer
from apps.wallets.models import Wallet, WalletTransaction
from django.db import transaction as db_transaction
from django.utils import timezone
from domain.utils import is_valid_tz_phone
from infrastructure.payment_gateways.factory import get_payment_gateway
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

logger = logging.getLogger(__name__)

# Fee constants
INSTANT_FEE_PERCENT = Decimal('0.01')  # 1%
MINIMUM_CASHOUT = Decimal('1000')  # TSh 1000 minimum

PROVIDERS = {
    'M_PESA': 'M-Pesa',
    'TIGO_PESA': 'Tigo Pesa',
    'AIRTEL_MONEY': 'Airtel Money',
    'HALOPESA': 'Halopesa',
    'MOBILE_MONEY': 'Mobile Money',
}

class WalletViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WalletSerializer

    def get_queryset(self):
        from apps.restaurants.models import SectionStaff
        staff_link = SectionStaff.objects.filter(user=self.request.user).select_related('section').first()
        if staff_link:
            return Wallet.objects.filter(section=staff_link.section).prefetch_related('transactions')
        return Wallet.objects.filter(user=self.request.user).prefetch_related('transactions')

    @action(detail=False, methods=['GET'])
    def me(self, request):
        from apps.restaurants.models import SectionStaff
        staff_link = SectionStaff.objects.filter(user=request.user).select_related('section').first()
        if staff_link:
            wallet, _ = Wallet.objects.get_or_create(section=staff_link.section)
        else:
            wallet, _ = Wallet.objects.get_or_create(user=request.user)
        wallet = Wallet.objects.prefetch_related('transactions').get(id=wallet.id)
        serializer = self.get_serializer(wallet)
        return Response(serializer.data)

    @action(detail=False, methods=['POST'])
    def calculate_fees(self, request):
        """Calculate fees for a given cashout amount"""
        try:
            amount_str = request.data.get('amount')
            if amount_str is None:
                return Response({"error": "Amount is required", "code": "MISSING_AMOUNT"}, status=400)
            
            amount = Decimal(str(amount_str))
        except (InvalidOperation, TypeError, ValueError):
            return Response({"error": "Invalid amount format", "code": "INVALID_AMOUNT"}, status=400)
        
        if amount <= 0:
            return Response({"error": "Amount must be greater than zero", "code": "AMOUNT_TOO_LOW"}, status=400)
        
        if amount > Decimal('10000000'):  # TSh 10 million max
            return Response({"error": "Amount exceeds maximum allowed", "code": "AMOUNT_TOO_HIGH"}, status=400)
        
        provider_key = request.data.get('provider', 'M_PESA').upper().replace(' ', '_')
        provider_name = PROVIDERS.get(provider_key, 'Mobile Money')

        instant_fee = amount * INSTANT_FEE_PERCENT
        total_fees = instant_fee
        net_amount = amount - total_fees

        logger.info(f"Fee calculation for user {request.user.id}: amount={amount}, provider={provider_name}, fees={total_fees}")

        return Response({
            "cashout_amount": float(amount),
            "instant_fee": float(instant_fee),
            "total_fees": float(total_fees),
            "net_amount": float(net_amount),
        })

    @action(detail=False, methods=['POST'])
    def cashout(self, request):
        logger.info(f"Cashout requested by user {request.user.id}")

        from apps.restaurants.models import SectionStaff
        staff_link = SectionStaff.objects.filter(user=request.user).select_related('section').first()
        if staff_link:
            wallet, _ = Wallet.objects.get_or_create(section=staff_link.section)
        else:
            wallet, _ = Wallet.objects.get_or_create(user=request.user)

        amount_str = request.data.get('amount')
        if amount_str is not None:
            try:
                amount = Decimal(str(amount_str))
            except (InvalidOperation, TypeError, ValueError):
                return Response({"error": "Invalid amount format", "code": "INVALID_AMOUNT"}, status=400)
            if amount <= 0:
                return Response({"error": "Amount must be greater than zero", "code": "AMOUNT_TOO_LOW"}, status=400)
            if amount > wallet.balance:
                return Response({"error": "Amount exceeds wallet balance", "code": "INSUFFICIENT_BALANCE"}, status=400)
        else:
            amount = wallet.balance

        if wallet.balance < MINIMUM_CASHOUT:
            logger.warning(f"Cashout failed for user {request.user.id}: balance {wallet.balance} below minimum {MINIMUM_CASHOUT}")
            return Response({
                "error": f"Minimum cashout amount is TSh {int(MINIMUM_CASHOUT)}",
                "code": "INSUFFICIENT_BALANCE",
                "current_balance": float(wallet.balance),
                "minimum_required": float(MINIMUM_CASHOUT),
            }, status=400)

        provider_key = request.data.get('provider', 'M_PESA').upper().replace(' ', '_')
        provider_name = PROVIDERS.get(provider_key, 'Mobile Money')
        phone = request.data.get('phone', '')

        # Server-side phone validation
        if not phone:
            return Response({"error": "Phone number is required", "code": "MISSING_PHONE"}, status=400)
        if not is_valid_tz_phone(phone):
            return Response({
                "error": "Invalid Tanzanian phone number. Use format: 0712 345 678",
                "code": "INVALID_PHONE",
            }, status=400)

        instant_fee = amount * INSTANT_FEE_PERCENT
        total_fees = instant_fee
        net_amount = amount - total_fees

        if net_amount <= 0:
            logger.warning(f"Cashout failed for user {request.user.id}: fees exceed amount")
            return Response({
                "error": "Balance too low to cover fees",
                "code": "FEES_EXCEED_AMOUNT",
            }, status=400)

        # Debit wallet first, then call gateway — inside atomic block.
        # If the gateway call fails, the database rollback reverses the debit.
        gateway = get_payment_gateway(phone=phone)
        reference = f"cashout_{request.user.id}_{timezone.now().strftime('%Y%m%d%H%M%S')}"

        try:
            with db_transaction.atomic():
                wallet = Wallet.objects.select_for_update().get(id=wallet.id)

                if wallet.balance < amount:
                    logger.error(
                        f"Cashout race detected for user {request.user.id}: "
                        f"wallet balance {wallet.balance} < requested {amount}"
                    )
                    return Response({"error": "Insufficient balance", "code": "INSUFFICIENT_BALANCE"}, status=409)
                if wallet.balance < MINIMUM_CASHOUT:
                    return Response({
                        "error": f"Minimum cashout amount is TSh {int(MINIMUM_CASHOUT)}",
                        "code": "INSUFFICIENT_BALANCE",
                    }, status=409)

                wallet.balance -= amount
                wallet.save()

                disbursement = gateway.disburse(
                    amount=net_amount,
                    account_info=phone,
                    reference=reference,
                    narration=f"Wallet cashout to {provider_name}",
                )

                if not disbursement.get('success'):
                    error_msg = disbursement.get('error', 'Disbursement failed')
                    logger.error(f"Cashout disbursement failed for user {request.user.id}: {error_msg}")
                    raise ValueError(f"Disbursement failed: {error_msg}")

                provider_reference = disbursement.get('provider_reference', '')

                WalletTransaction.objects.create(
                    wallet=wallet,
                    amount=net_amount,
                    tx_type='DEBIT',
                    description=f"Cashout to {provider_name}",
                    created_at=timezone.now(),
                )

                WalletTransaction.objects.create(
                    wallet=wallet,
                    amount=total_fees,
                    tx_type='DEBIT',
                    description=f"Cashout processing fees ({provider_name})",
                    created_at=timezone.now(),
                )
        except ValueError as e:
            return Response({
                "error": f"Cashout failed: {e}",
                "code": "DISBURSEMENT_FAILED",
            }, status=502)

        logger.info(
            f"Cashout successful for user {request.user.id}: "
            f"amount={amount}, net={net_amount}, provider={provider_name}, "
            f"phone={phone}, ref={provider_reference}"
        )

        return Response({
            "message": "Cashout successful",
            "cashout_amount": float(amount),
            "instant_fee": float(instant_fee),
            "provider_name": provider_name,
            "total_fees": float(total_fees),
            "net_amount": float(net_amount),
            "provider_reference": provider_reference,
        })

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
