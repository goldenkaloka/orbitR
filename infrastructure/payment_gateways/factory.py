from .airpay import AirpayGateway

def get_payment_gateway(provider=None):
    """
    Returns the configured payment gateway.
    Airpay is the only supported provider.
    """
    return AirpayGateway()
