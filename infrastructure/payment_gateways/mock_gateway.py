import logging
import uuid
from .base import PaymentGateway

logger = logging.getLogger(__name__)

class MockPaymentGateway(PaymentGateway):
    """
    Mock Gateway for Development/Testing.
    Implements the PaymentGateway interface but simulates instantaneous successes.
    """
    def collect(self, amount, phone, order_reference, **kwargs):
        """
        Simulates a successful collection immediately.
        We return instant_success=True so the application knows to bypass any WebViews.
        """
        mock_transaction_id = f"MOCK_TXN_{uuid.uuid4().hex[:8].upper()}"
        logger.info(f"MockPaymentGateway: Collection initiated for {order_reference}, amount: {amount}")
        
        return {
            'success': True,
            'instant_success': True,
            'transactionId': mock_transaction_id,
            'message': 'Mock payment completed successfully'
        }

    def verify_transaction(self, order_reference=None, ap_transactionid=None):
        return {
            'success': True, 
            'status': 'SUCCESS', 
            'message': 'Mock verify success'
        }

    def split_settlement(self, airpay_id, rrn, splits: list, **kwargs):
        logger.info(f"MockPaymentGateway: Split settlement for {airpay_id}")
        return {'success': True, 'message': 'Mock Split Successful'}

    def get_status(self, provider_reference):
        return self.verify_transaction(ap_transactionid=provider_reference)

    def disburse(self, amount, account_info, reference, **kwargs):
        return {'success': True, 'payout_id': f'MOCK_DISB_{reference}'}

    def refund(self, airpay_id, amount):
        return {'success': True, 'refund_id': f'MOCK_REFUND_{airpay_id}'}
