from django.conf import settings

from .airpay import AirpayGateway
from .mock import MockGateway


def get_payment_gateway(provider=None, phone=None):
    """
    Returns the configured payment gateway.
    Supports 'mock' provider or '0000000000' phone number for testing.
    """
    is_mock_phone = str(phone).endswith('0000000000')
    if provider == 'mock' or getattr(settings, 'MOCK_PAYMENTS', False) or is_mock_phone:
        return MockGateway()
        
    return AirpayGateway()
