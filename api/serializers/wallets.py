from apps.wallets.models import Wallet, WalletTransaction
from rest_framework import serializers


class TransactionSerializer(serializers.ModelSerializer):
    wallet_label = serializers.SerializerMethodField()

    class Meta:
        model = WalletTransaction
        fields = [
            'id', 'amount', 'tx_type', 'transaction_type', 'status',
            'description', 'reference', 'balance_after', 'wallet_label',
            'created_at',
        ]

    def get_wallet_label(self, obj):
        if obj.wallet.section_id:
            return obj.wallet.section.name
        return 'Personal'


class WalletSerializer(serializers.ModelSerializer):
    recent_transactions = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = ['id', 'balance', 'pending_balance', 'updated_at', 'recent_transactions']

    def get_recent_transactions(self, obj):
        txs = obj.transactions.all()[:10]
        return TransactionSerializer(txs, many=True).data
