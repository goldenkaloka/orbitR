from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from apps.wallets.models import Wallet, WalletTransaction
from api.serializers.wallets import WalletSerializer, TransactionSerializer

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
    def cashout(self, request):
        # This is where we would call the EasyPay Payout API
        wallet, _ = Wallet.objects.get_or_create(user=request.user)
        if wallet.balance <= 0:
            return Response({"error": "Insufficient balance"}, status=400)
            
        # Mocking the payout process for now
        amount = wallet.balance
        wallet.balance = 0
        wallet.save()
        
        WalletTransaction.objects.create(
            wallet=wallet,
            amount=amount,
            tx_type='DEBIT',
            description="Cashout to Mobile Money"
        )
        
        return Response({
            "message": "Cashout request submitted successfully",
            "amount": float(amount)
        })
