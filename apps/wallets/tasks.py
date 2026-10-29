import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from celery import shared_task
from apps.wallets.models import Wallet, WalletPayout
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

RECONCILE_BATCH_SIZE = 50
AUTO_PAYOUT_BATCH_SIZE = 500
AUTO_PAYOUT_PROVIDER = 'M_PESA'


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


def _auto_payout_threshold():
    try:
        return Decimal(str(getattr(settings, 'DRIVER_AUTO_PAYOUT_THRESHOLD', '30000')))
    except (InvalidOperation, TypeError, ValueError):
        logger.error('[auto-payout] DRIVER_AUTO_PAYOUT_THRESHOLD is not a number')
        return None


@shared_task(name='apps.wallets.tasks.auto_payout_driver_wallets')
def auto_payout_driver_wallets():
    """Fan out one payout task per driver whose wallet reached the threshold.

    The sweep only enqueues work: a gateway call can spend its whole 30-second
    timeout, so paying every driver inside this task would exceed
    CELERY_TASK_TIME_LIMIT. Each driver's payout runs as its own task.
    """
    if not getattr(settings, 'DRIVER_AUTO_PAYOUT_ENABLED', True):
        return {'status': 'disabled'}

    threshold = _auto_payout_threshold()
    if threshold is None:
        return {'status': 'bad_threshold'}

    day = timezone.localdate().isoformat()
    driver_ids = list(
        Wallet.objects.filter(
            user__role='driver',
            user__is_active=True,
            balance__gte=threshold,
        ).order_by('id').values_list('user_id', flat=True)[:AUTO_PAYOUT_BATCH_SIZE]
    )

    for driver_id in driver_ids:
        payout_driver_wallet.delay(str(driver_id), day)

    logger.info(
        f'[auto-payout] {day}: queued {len(driver_ids)} driver payout(s) '
        f'at threshold {threshold}'
    )
    return {
        'status': 'queued',
        'day': day,
        'threshold': str(threshold),
        'queued': len(driver_ids),
    }


@shared_task(name='apps.wallets.tasks.payout_driver_wallet')
def payout_driver_wallet(user_id, day=None):
    """Pay a driver's whole wallet balance for ``day``.

    The date in the idempotency key is what makes a repeated sweep safe: the
    second call returns the first payout instead of sending the money twice.
    """
    from apps.users.models import User
    from domain.services.wallet_payout_services import CashoutError, CashoutService

    day = day or timezone.localdate().isoformat()
    user = User.objects.filter(
        id=user_id, role='driver', is_active=True,
    ).first()
    if user is None:
        return {'status': 'missing'}

    try:
        payout = CashoutService.execute(
            user=user,
            amount=None,  # the whole balance
            phone=user.phone,
            provider=AUTO_PAYOUT_PROVIDER,
            idempotency_key=f'auto-payout:{user.id}:{day}',
        )
    except CashoutError as exc:
        # In-flight cashouts and balances below the minimum are expected.
        logger.info(f'[auto-payout] driver {user.id}: {exc.code} — {exc.message}')
        return {'status': 'skipped', 'code': exc.code}
    except Exception as exc:
        logger.exception(f'[auto-payout] driver {user.id} failed')
        return {'status': 'failed', 'error': str(exc)}

    logger.info(
        f'[auto-payout] driver {user.id}: payout {payout.id} '
        f'{payout.amount} -> {payout.destination_phone} ({payout.status})'
    )
    return {'status': payout.status, 'payout_id': str(payout.id)}
