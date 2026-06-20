from django.contrib import admin
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from .models import FailedPayout, Payment, Transaction


class TransactionInline(TabularInline):
    model = Transaction
    extra = 0
    fields = ('transaction_type', 'from_wallet', 'to_wallet', 'amount', 'status', 'created_at')
    readonly_fields = ('created_at',)
    raw_id_fields = ('from_wallet', 'to_wallet')
    show_change_link = True


class FailedPayoutInline(TabularInline):
    model = FailedPayout
    extra = 0
    fields = ('to_wallet', 'amount', 'transaction_type', 'reason', 'retry_count', 'created_at')
    readonly_fields = ('created_at',)
    raw_id_fields = ('to_wallet',)
    show_change_link = True


@admin.register(Payment)
class PaymentAdmin(ModelAdmin):
    list_display = (
        'short_id', 'order', 'amount', 'show_status',
        'provider_reference', 'transaction_count', 'failed_count', 'created_at',
    )
    list_filter = ('status', 'created_at')
    search_fields = ('id', 'order__id', 'provider_reference')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 25
    raw_id_fields = ('order',)
    readonly_fields = ('id', 'created_at')
    inlines = [TransactionInline, FailedPayoutInline]

    fieldsets = (
        ('Payment Info', {'fields': ('id', 'order', 'amount', 'status')}),
        ('Provider', {'fields': ('provider_reference',)}),
        ('Timestamps', {'fields': ('created_at',)}),
    )

    @display(description="Payment ID")
    def short_id(self, obj):
        return str(obj.id)[:8] + '…'

    @display(
        description="Status",
        ordering="status",
        label={
            "INITIATED": "info",
            "COLLECTED": "success",
            "DISBURSED": "success",
            "FAILED": "danger",
            "PARTIALLY_DISBURSED": "warning",
        },
    )
    def show_status(self, obj):
        return obj.status

    @display(description="Txns")
    def transaction_count(self, obj):
        return obj.transactions.count()

    @display(
        description="Failed",
        label={
            "0": "success",
        },
    )
    def failed_count(self, obj):
        count = obj.failed_payouts.count()
        if count > 0:
            return f"⚠ {count}"
        return "0"

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            .select_related('order')
            .prefetch_related('transactions', 'failed_payouts')
        )


@admin.register(Transaction)
class TransactionAdmin(ModelAdmin):
    list_display = (
        'short_id', 'payment', 'show_type', 'from_wallet',
        'to_wallet', 'amount', 'show_status', 'created_at',
    )
    list_filter = ('status', 'transaction_type', 'created_at')
    search_fields = (
        'id', 'payment__id',
        'from_wallet__username', 'to_wallet__username',
    )
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 25
    raw_id_fields = ('payment', 'from_wallet', 'to_wallet')
    readonly_fields = ('id', 'created_at')

    @display(description="Txn ID")
    def short_id(self, obj):
        return str(obj.id)[:8] + '…'

    @display(description="Type", label=True)
    def show_type(self, obj):
        return obj.transaction_type

    @display(
        description="Status",
        ordering="status",
        label={
            "PENDING": "warning",
            "COMPLETED": "success",
            "FAILED": "danger",
        },
    )
    def show_status(self, obj):
        return obj.status

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('payment', 'from_wallet', 'to_wallet')


@admin.register(FailedPayout)
class FailedPayoutAdmin(ModelAdmin):
    list_display = (
        'short_id', 'payment', 'to_wallet', 'amount',
        'show_type', 'retry_count', 'reason_short', 'created_at',
    )
    list_filter = ('transaction_type', 'retry_count', 'created_at')
    search_fields = ('id', 'payment__id', 'to_wallet__username', 'reason')
    ordering = ('-created_at',)
    date_hierarchy = 'created_at'
    list_per_page = 25
    raw_id_fields = ('payment', 'to_wallet')
    readonly_fields = ('id', 'created_at')

    actions = ['reset_retry_count']

    @display(description="ID")
    def short_id(self, obj):
        return str(obj.id)[:8] + '…'

    @display(description="Type", label=True)
    def show_type(self, obj):
        return obj.transaction_type

    @display(description="Reason")
    def reason_short(self, obj):
        if obj.reason and len(obj.reason) > 60:
            return obj.reason[:60] + '…'
        return obj.reason or '-'

    @admin.action(description='🔄 Reset retry count to 0')
    def reset_retry_count(self, request, queryset):
        updated = queryset.update(retry_count=0)
        self.message_user(request, f'{updated} payout(s) retry count reset.')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('payment', 'to_wallet')
