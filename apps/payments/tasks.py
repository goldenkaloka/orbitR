from celery import shared_task
from django.db import transaction
from .models import FailedPayout
from domain.services.payment_services import ReleasePaymentService
import logging

logger = logging.getLogger(__name__)

@shared_task(name='apps.payments.tasks.retry_failed_payouts')
def retry_failed_payouts():
    """
    Background task to retry failed split settlements.
    Retries payouts with retry_count < 5.
    """
    failed_payouts = FailedPayout.objects.filter(retry_count__lt=5)
    
    for payout in failed_payouts:
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
            
        except Exception as e:
            logger.error(f"Retry failed for payout {payout.id}: {str(e)}")
            payout.reason = f"Last failure: {str(e)}"
            payout.save(update_fields=['reason'])

@shared_task(name='apps.payments.tasks.cleanup_expired_payments')
def cleanup_expired_payments():
    """
    Cancel payments that have been stuck in INITIATED for more than 30 mins.
    """
    from apps.payments.models import Payment
    from django.utils import timezone
    from datetime import timedelta
    
    expiry_time = timezone.now() - timedelta(minutes=30)
    stale_payments = Payment.objects.filter(status='INITIATED', created_at__lt=expiry_time)
    
    for payment in stale_payments:
        try:
            logger.info(f"Expiring stale payment {payment.id}")
            payment.status = 'FAILED' 
            payment.save(update_fields=['status'])
        except Exception as e:
            logger.error(f"Failed to expire payment {payment.id}: {str(e)}")
