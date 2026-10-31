"""What a real mobile-money payout costs the platform.

Every ``gateway.disburse()`` call moves money out of the platform's float and
ClickPesa keeps a fee. That fee is a *fixed TZS amount per transfer*, not a
percentage, so it is invisible in a commission-based revenue model: a 5%
commission on a TZS 16,000 order (800) is almost entirely consumed by the
~680 fee on the 15,200 payout that settles it.

Until this module existed nothing recorded those fees, so ``platform_cut``
booked profit that had already left the account. The numbers below make the
leak visible; they do not change what the gateway charges.

Two sources, recorded in ``gateway_fee_source``:

``GATEWAY``
    The provider returned the fee in its response. Always preferred.
``ESTIMATE``
    Derived from the published tier table (``GATEWAY_PAYOUT_FEE_TIERS``).

Estimates err towards *overstating* the fee: an amount that falls in a gap in
the published table is charged the next higher tier, never the lower one. A
revenue report that is slightly pessimistic is safer than one that books money
the platform does not have.
"""

from decimal import Decimal, InvalidOperation

from django.conf import settings

import logging

logger = logging.getLogger(__name__)

FEE_SOURCE_GATEWAY = 'GATEWAY'
FEE_SOURCE_ESTIMATE = 'ESTIMATE'
FEE_SOURCE_NONE = ''

# Published ClickPesa mobile-money payout schedule (TZS). Amounts between two
# published bands are billed at the next band up — see the module docstring.
DEFAULT_PAYOUT_FEE_TIERS = (
    (Decimal('1000'), Decimal('1999.99'), Decimal('72')),
    (Decimal('3000'), Decimal('3999.99'), Decimal('116')),
    (Decimal('8000'), Decimal('9999.99'), Decimal('430')),
    (Decimal('10000'), Decimal('14999.99'), Decimal('642')),
    (Decimal('15000'), Decimal('19999.99'), Decimal('680')),
    (Decimal('30000'), Decimal('39999.99'), Decimal('980')),
    (Decimal('50000'), Decimal('99999.99'), Decimal('1460')),
)

# Keys a gateway might use for "what this transfer cost you".
_FEE_KEYS = (
    'fee', 'fees', 'charge', 'charges', 'transactionFee', 'transaction_fee',
    'serviceFee', 'service_fee', 'cost',
)

ZERO = Decimal('0')


def _as_decimal(value):
    if value is None or value == '':
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _normalise(raw):
    """Accept [(min, max, fee)] or [{'min':..,'max':..,'fee':..}]."""
    tiers = []
    for entry in raw or ():
        if isinstance(entry, dict):
            low = _as_decimal(entry.get('min'))
            high = _as_decimal(entry.get('max'))
            fee = _as_decimal(entry.get('fee'))
        else:
            try:
                low, high, fee = entry
            except (TypeError, ValueError):
                continue
            low, high, fee = _as_decimal(low), _as_decimal(high), _as_decimal(fee)
        if low is None or high is None or fee is None:
            continue
        tiers.append((low, high, fee))
    return tuple(sorted(tiers, key=lambda tier: tier[0]))


def payout_fee_tiers(provider=None):
    """The tier table for ``provider``, falling back to the default table.

    ``GATEWAY_PAYOUT_FEE_TIERS`` may be a flat list or a dict keyed by provider
    (with an optional ``'default'`` key), so a second gateway with a different
    schedule can be added without touching this module.
    """
    configured = getattr(settings, 'GATEWAY_PAYOUT_FEE_TIERS', None)
    if isinstance(configured, dict):
        key = (provider or '').strip().lower()
        tiers = _normalise(configured.get(key) or configured.get('default'))
        return tiers or DEFAULT_PAYOUT_FEE_TIERS
    tiers = _normalise(configured)
    return tiers or DEFAULT_PAYOUT_FEE_TIERS


def estimate_payout_fee(amount, provider=None):
    """Estimated gateway fee for paying ``amount`` out, in TZS."""
    amount = _as_decimal(amount)
    if amount is None or amount <= 0:
        return ZERO

    tiers = payout_fee_tiers(provider)
    if not tiers:
        return ZERO

    for low, high, fee in tiers:
        if low <= amount <= high:
            return fee

    # Between two published bands: bill the next band up, not the one below.
    for low, _high, fee in tiers:
        if amount < low:
            return fee

    # Above the top band the schedule is undocumented; the top fee is the only
    # defensible floor. Keep it configurable rather than guessing a percentage.
    top_low, top_high, top_fee = tiers[-1]
    if amount > top_high:
        logger.warning(
            "[gateway_fees] Payout of %s TZS is above the highest published "
            "band (%s-%s); using the %s TZS top-tier fee as a floor. Add the "
            "missing band to GATEWAY_PAYOUT_FEE_TIERS once ClickPesa publishes it.",
            amount, top_low, top_high, top_fee,
        )
    return top_fee


def fee_from_gateway_result(result):
    """The fee the provider actually reported, or None when it did not say."""
    if not isinstance(result, dict):
        return None
    for container in (result, result.get('raw_data') or {}):
        if not isinstance(container, dict):
            continue
        for key in _FEE_KEYS:
            fee = _as_decimal(container.get(key))
            if fee is not None and fee >= 0:
                return fee
    return None


def resolve_payout_fee(result, amount, provider=None):
    """``(fee, source)`` for one payout attempt.

    A failed payout costs nothing, so it resolves to zero with no source.
    """
    if not isinstance(result, dict) or not result.get('success'):
        return ZERO, FEE_SOURCE_NONE

    reported = fee_from_gateway_result(result)
    if reported is not None:
        return reported.quantize(Decimal('0.01')), FEE_SOURCE_GATEWAY
    return estimate_payout_fee(amount, provider).quantize(Decimal('0.01')), \
        FEE_SOURCE_ESTIMATE
