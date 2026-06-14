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
PROVIDER_FIXED_FEE = Decimal('220')  # TSh 220
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
        total_fees = instant_fee + PROVIDER_FIXED_FEE
        net_amount = amount - total_fees
        
        logger.info(f"Fee calculation for user {request.user.id}: amount={amount}, provider={provider_name}, fees={total_fees}")
        
        return Response({
            "cashout_amount": float(amount),
            "instant_fee": float(instant_fee),
            "provider_fee": float(PROVIDER_FIXED_FEE),
            "provider_name": provider_name,
            "total_fees": float(total_fees),
            "net_amount": float(max(net_amount, 0)),
        })

    @action(detail=False, methods=['POST'])
    @transaction.atomic
    def cashout(self, request):
        logger.info(f"Cashout requested by user {request.user.id}")
        
        from apps.restaurants.models import SectionStaff
        staff_link = SectionStaff.objects.filter(user=request.user).select_related('section').first()
        if staff_link:
            wallet, _ = Wallet.objects.select_for_update().get_or_create(section=staff_link.section)
        else:
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=request.user)
        
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
        
        instant_fee = amount * INSTANT_FEE_PERCENT
        total_fees = instant_fee + PROVIDER_FIXED_FEE
        net_amount = amount - total_fees
        
        if net_amount <= 0:
            logger.warning(f"Cashout failed for user {request.user.id}: fees exceed amount")
            return Response({
                "error": "Balance too low to cover fees",
                "code": "FEES_EXCEED_AMOUNT",
            }, status=400)
        
        wallet.balance -= amount
        wallet.save()
        
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
        
        logger.info(f"Cashout successful for user {request.user.id}: amount={amount}, provider={provider_name}, phone={phone}, net={net_amount}")
        
        return Response({
            "message": "Cashout request submitted successfully",
            "cashout_amount": float(amount),
            "instant_fee": float(instant_fee),
            "provider_fee": float(PROVIDER_FIXED_FEE),
            "provider_name": provider_name,
            "total_fees": float(total_fees),
            "net_amount": float(net_amount),
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
