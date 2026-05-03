from django.conf import settings
from .airpay import AirpayGateway
from .mock_gateway import MockPaymentGateway

def get_payment_gateway(provider=None):
    """
    Factory function to return the appropriate payment gateway based on environment config.
    """
    allow_mock = settings.AIRPAY_CONFIG.get('ALLOW_MOCK_IN_PROD', False)
    use_mock = settings.AIRPAY_CONFIG.get('MOCK', False)
    
    # If not debug and we don't explicitly allow mock in prod, force Airpay.
    if not settings.DEBUG and not allow_mock:
        return AirpayGateway()
        
    if use_mock or provider == 'Mock':
        return MockPaymentGateway()
        
    return AirpayGateway()
