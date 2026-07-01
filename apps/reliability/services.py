import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from .models import ReliabilityRecord, Strike

logger = logging.getLogger(__name__)

STRIKE_WEIGHTS = {
    'customer': {
        'cancel_created': 1,
        'cancel_confirmed': 2,
    },
    'vendor': {
        'cancel_confirmed': 3,
        'cancel_preparing': 5,
        'no_show_vendor': 10,
    },
    'driver': {
        'cancel_assigned': 2,
        'cancel_picked': 5,
        'no_deliver_driver': 10,
    },
}

DECAY_DAYS = {
    'customer': {
        'cancel_created': 30,
        'cancel_confirmed': 30,
    },
    'vendor': {
        'cancel_confirmed': 45,
        'cancel_preparing': 60,
        'no_show_vendor': 90,
    },
    'driver': {
        'cancel_assigned': 30,
        'cancel_picked': 60,
        'no_deliver_driver': 90,
    },
}

THRESHOLDS = [
    (0, None, 'Good standing'),
    (3, 'warning', 'Reliability warning — excessive cancellations'),
    (6, 'soft_restrict', 'Cancel fee deducted from refund (5%)'),
    (10, 'hard_restrict', 'Cancel fee deducted (15%), priority lowered'),
    (15, 'suspended', 'Account temporarily suspended for 7 days'),
]

CANCEL_FEES = {
    'soft_restrict': Decimal('0.05'),
    'hard_restrict': Decimal('0.15'),
}


def get_reliability(user, role):
    record, _ = ReliabilityRecord.objects.get_or_create(user=user, role=role)
    _recalc_score(record)
    return record


def _recalc_score(record):
    active = record.strikes.filter(waived=False, expires_at__gt=timezone.now())
    record.score = sum(s.weight for s in active)
    record.save(update_fields=['score'])
    return record.score


def get_threshold(score):
    for level, action, label in reversed(THRESHOLDS):
        if score >= level:
            return level, action, label
    return THRESHOLDS[0]


def should_waive(order):
    """Waive strike if the order has a dispute resolved in the customer's favor."""
    if not order:
        return False
    from apps.orders.models import Dispute
    return Dispute.objects.filter(
        order=order,
        raised_by=order.customer,
        status__in=['RESOLVED_REFUND', 'RESOLVED_PARTIAL'],
    ).exists()


def add_strike(user, role, reason, order=None):
    record = get_reliability(user, role)
    weights = STRIKE_WEIGHTS.get(role, {})
    decay = DECAY_DAYS.get(role, {})
    weight = weights.get(reason, 1)
    days = decay.get(reason, 30)

    waived = False
    if order and role == 'customer':
        waived = should_waive(order)

    with transaction.atomic():
        Strike.objects.create(
            record=record,
            reason=reason,
            weight=weight,
            waived=waived,
            order=order,
            expires_at=timezone.now() + timedelta(days=days),
        )
        if not waived:
            record.score += weight
            record.save(update_fields=['score'])

    logger.info(
        f"Strike added: user={user.id} role={role} reason={reason} "
        f"weight={weight} waived={waived} score={record.score}"
    )
    return record


def get_cancel_fee_percent(score):
    _level, action, _label = get_threshold(score)
    return CANCEL_FEES.get(action, Decimal('0'))
