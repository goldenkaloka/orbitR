import logging
from datetime import timedelta

from celery import shared_task
from apps.wallets.models import WalletPayout
from django.utils import timezone

logger = logging.getLogger(__name__)

RECONCILE_BATCH_SIZE = 50


@shared_task(name='apps.wallets.tasks.reconcile_wallet_payouts')
def reconcile_wallet_payouts():
    """Settle cashouts whose gateway outcome we never learned.

    A payout left PROCESSING for more than five minutes is either a crashed
    worker or a timeout. UNKNOWN payouts are only polled when the gateway gave
    us a reference; without one the money may already have moved, so those are
    left for manual review.
    """
    from domain.services.wallet_payout_services import reconcile_payout

    stale_cutoff = timezone.now() - timedelta(minutes=5)
    payout_ids = list(
        WalletPayout.objects.filter(
            status='PROCESSING', updated_at__lt=stale_cutoff,
        ).order_by('updated_at').values_list('id', flat=True)[:RECONCILE_BATCH_SIZE]
    )
    payout_ids += list(
        WalletPayout.objects.filter(
            status='UNKNOWN', provider_reference__gt='',
        ).order_by('updated_at').values_list('id', flat=True)[:RECONCILE_BATCH_SIZE]
    )

    reconciled = 0
    for payout_id in dict.fromkeys(payout_ids):
        try:
            reconcile_payout(payout_id)
            reconciled += 1
        except Exception:
            logger.exception(f"Failed to reconcile wallet payout {payout_id}")

    if reconciled:
        logger.info(f"Reconciled {reconciled} wallet payout(s)")
