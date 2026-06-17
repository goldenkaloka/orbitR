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
            # Bug 15 Fix: Idempotency check — skip if already disbursed
            if payout.payment.status == 'DISBURSED':
                logger.info(f"Payout {payout.id} already disbursed, cleaning up stale record.")
                payout.delete()
                processed += 1
                continue

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
    Cancel payments stuck in INITIATED or INPROCESS for more than 5 mins
    (USSD push timeout is ~120s, so 5min is a generous safety net).
    Before cancelling, poll the payment gateway status. If it was completed, recover it.
    """
    from apps.payments.models import Payment
    from django.utils import timezone
    from datetime import timedelta
    from infrastructure.payment_gateways.factory import get_payment_gateway
    from domain.services.payment_services import CompletePaymentService
    
    expiry_time = timezone.now() - timedelta(minutes=5)
    stuck_payments = Payment.objects.filter(status__in=['INITIATED', 'INPROCESS'], created_at__lt=expiry_time)
    
    expired_count = 0
    recovered_count = 0
    
    for payment in stuck_payments:
        try:
            gateway = get_payment_gateway(phone=payment.payable_id)
            
            if payment.provider_reference:
                status_res = gateway.get_status(provider_reference=payment.provider_reference)
                if status_res.get('success') and status_res.get('status') == 'SUCCESS':
                    CompletePaymentService.execute(
                        payment_id=payment.id,
                        provider_reference=payment.provider_reference,
                        success=True
                    )
                    recovered_count += 1
                    logger.info(f"Recovered stuck payment {payment.id} via status polling")
                    continue
            
            payment.status = 'FAILED'
            payment.save()
            expired_count += 1
            logger.info(f"Expired stuck payment {payment.id} to FAILED")
        except Exception as e:
            logger.error(f"Error checking status for payment {payment.id}: {str(e)}")
            
    if expired_count or recovered_count:
        logger.info(f"Expired payment cleanup complete. Expired: {expired_count}, Recovered: {recovered_count}")
