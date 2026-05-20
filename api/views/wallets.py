import logging
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from decimal import Decimal, InvalidOperation
from django.utils import timezone
from apps.wallets.models import Wallet, WalletTransaction
from api.serializers.wallets import WalletSerializer, TransactionSerializer

logger = logging.getLogger(__name__)

# Fee constants
INSTANT_FEE_PERCENT = Decimal('0.01')  # 1%
MPESA_FIXED_FEE = Decimal('220')  # TSh 220
MINIMUM_CASHOUT = Decimal('1000')  # TSh 1000 minimum

class WalletViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = WalletSerializer

    def get_queryset(self):
        return Wallet.objects.filter(user=self.request.user)

    @action(detail=False, methods=['GET'])
    def me(self, request):
        wallet, _ = Wallet.objects.get_or_create(user=request.user)
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
        
        instant_fee = amount * INSTANT_FEE_PERCENT
        total_fees = instant_fee + MPESA_FIXED_FEE
        net_amount = amount - total_fees
        
        logger.info(f"Fee calculation for user {request.user.id}: amount={amount}, fees={total_fees}")
        
        return Response({
            "cashout_amount": float(amount),
            "instant_fee": float(instant_fee),
            "mpesa_fee": float(MPESA_FIXED_FEE),
            "total_fees": float(total_fees),
            "net_amount": float(max(net_amount, 0)),
        })

    @action(detail=False, methods=['POST'])
    @transaction.atomic
    def cashout(self, request):
        logger.info(f"Cashout requested by user {request.user.id}")
        
        try:
            wallet = Wallet.objects.select_for_update().get(user=request.user)
        except Wallet.DoesNotExist:
            wallet = Wallet.objects.create(user=request.user)
            
        if wallet.balance < MINIMUM_CASHOUT:
            logger.warning(f"Cashout failed for user {request.user.id}: balance {wallet.balance} below minimum {MINIMUM_CASHOUT}")
            return Response({
                "error": f"Minimum cashout amount is TSh {int(MINIMUM_CASHOUT)}",
                "code": "INSUFFICIENT_BALANCE",
                "current_balance": float(wallet.balance),
                "minimum_required": float(MINIMUM_CASHOUT),
            }, status=400)
            
        amount = wallet.balance
        
        instant_fee = amount * INSTANT_FEE_PERCENT
        total_fees = instant_fee + MPESA_FIXED_FEE
        net_amount = amount - total_fees
        
        if net_amount <= 0:
            logger.warning(f"Cashout failed for user {request.user.id}: fees exceed amount")
            return Response({
                "error": "Balance too low to cover fees",
                "code": "FEES_EXCEED_AMOUNT",
            }, status=400)
        
        wallet.balance = 0
        wallet.save()
        
        WalletTransaction.objects.create(
            wallet=wallet,
            amount=amount,
            tx_type='DEBIT',
            description="Cashout to Mobile Money",
            created_at=timezone.now(),
        )
        
        WalletTransaction.objects.create(
            wallet=wallet,
            amount=total_fees,
            tx_type='DEBIT',
            description="Cashout processing fees",
            created_at=timezone.now(),
        )
        
        logger.info(f"Cashout successful for user {request.user.id}: amount={amount}, net={net_amount}")
        
        return Response({
            "message": "Cashout request submitted successfully",
            "cashout_amount": float(amount),
            "instant_fee": float(instant_fee),
            "mpesa_fee": float(MPESA_FIXED_FEE),
            "total_fees": float(total_fees),
            "net_amount": float(net_amount),
        })
