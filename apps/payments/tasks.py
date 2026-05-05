from celery import shared_task
from django.db import transaction
from .models import FailedPayout
from domain.services.payment_services import ReleasePaymentService
import logging

logger = logging.getLogger(__name__)
RETRY_BATCH_SIZE = 100
MAX_RETRY_COUNT = 5

@shared_task(name='apps.payments.tasks.retry_failed_payouts')
def retry_failed_payouts():
    """
    Background task to retry failed split settlements.
    Retries payouts with retry_count < 5.
    """
    with transaction.atomic():
        payout_ids = list(
            FailedPayout.objects.select_for_update(skip_locked=True)
            .filter(retry_count__lt=MAX_RETRY_COUNT)
            .order_by('created_at')
            .values_list('id', flat=True)[:RETRY_BATCH_SIZE]
        )

    processed = 0
    for payout in FailedPayout.objects.filter(id__in=payout_ids).select_related('payment'):
        try:
            logger.info(f"Retrying payout for payment {payout.payment.id} (Attempt {payout.retry_count + 1})")
            
            # Increment retry count first to prevent infinite loop on crash
            payout.retry_count += 1
            payout.save(update_fields=['retry_count'])
            
            # Re-execute release service
            ReleasePaymentService.execute(payout.payment.id)
            
            # If successful (no exception), delete the failure record
            payout.delete()
            logger.info(f"Successfully retried payout for payment {payout.payment.id}")
            processed += 1
            
        except Exception as e:
            logger.error(f"Retry failed for payout {payout.id}: {str(e)}")
            payout.reason = f"Last failure: {str(e)}"
            payout.save(update_fields=['reason'])
    if processed:
        logger.info(f"Processed {processed} payout retries")

@shared_task(name='apps.payments.tasks.cleanup_expired_payments')
def cleanup_expired_payments():
    """
    Cancel payments that have been stuck in INITIATED for more than 30 mins.
    """
    from apps.payments.models import Payment
    from django.utils import timezone
    from datetime import timedelta
    
    expiry_time = timezone.now() - timedelta(minutes=30)
    updated = Payment.objects.filter(status='INITIATED', created_at__lt=expiry_time).update(status='FAILED')
    if updated:
        logger.info(f"Expired {updated} stale payments")
