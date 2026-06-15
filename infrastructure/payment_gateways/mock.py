from .base import PaymentGateway

class MockGateway(PaymentGateway):
    """
    Mock Payment Gateway for testing.
    Always returns success without hitting any external APIs.
    """
    def collect(self, amount, phone, order_reference, **kwargs):
        return {
            'success': True,
            'provider_reference': f"MOCK_TXN_{order_reference}",
            'message': 'Mock USSD Push initiated successfully',
            'instant_success': True, # Signal for the service to auto-complete
            'is_push': False
        }

    def disburse(self, amount, account_info, reference, **kwargs):
        return {'success': True, 'provider_reference': f"MOCK_DISB_{reference}"}

    def get_status(self, provider_reference=None, orderid=None):
        return {
            'success': True,
            'status': 'SUCCESS',
            'raw_status': '200',
            'data': {'transaction_status': '200'}
        }

    def refund(self, transaction_id, amount, **kwargs):
        return {'success': True, 'refund_id': f"MOCK_REFUND_{transaction_id}"}

    def split_settlement(self, airpay_id, rrn, *args, **kwargs):
        return {'success': True}
