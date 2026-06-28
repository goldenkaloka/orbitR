import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def expire_strikes():
    from .models import Strike
    expired = Strike.objects.filter(
        waived=False,
        expires_at__lte=timezone.now(),
    ).select_related('record')
    count = 0
    for strike in expired:
        strike.record.score = max(0, strike.record.score - strike.weight)
        strike.record.save(update_fields=['score'])
        count += 1
    if count:
        logger.info(f"Expired {count} strike(s), recalculated scores")
