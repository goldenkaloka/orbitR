from rest_framework import serializers
from apps.wallets.models import Wallet, Transaction

class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = ['id', 'amount', 'tx_type', 'description', 'created_at']

class WalletSerializer(serializers.ModelSerializer):
    recent_transactions = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = ['id', 'balance', 'pending_balance', 'updated_at', 'recent_transactions']

    def get_recent_transactions(self, obj):
        txs = obj.transactions.all()[:10]
        return TransactionSerializer(txs, many=True).data
