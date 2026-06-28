from django.conf import settings

from .airpay import AirpayGateway
from .mock import MockGateway


def get_payment_gateway(provider=None, phone=None):
    """
    Returns the configured payment gateway.
    Mock mode is controlled via Django settings only — never via user-supplied data.
    """
    if getattr(settings, 'MOCK_PAYMENTS', False):
        return MockGateway()

    return AirpayGateway()
