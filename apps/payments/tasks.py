import logging

from celery import shared_task
from django.db import transaction
from domain.services.payment_services import ReleasePaymentService

from .models import FailedPayout

logger = logging.getLogger(__name__)
RETRY_BATCH_SIZE = 100
MAX_RETRY_COUNT = 5
MAX_REFUND_RETRY_COUNT = 10

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

@shared_task(name='apps.payments.tasks.retry_failed_refunds')
def retry_failed_refunds():
    """
    Finish refunds the gateway refused at cancellation time.

    RefundPaymentService treats a gateway refusal as "nothing refunded" and
    queues a FailedRefund row. Until somebody retries it the customer stays
    paid for a cancelled order, so this sweep drives the refund to completion
    and gives up only after MAX_REFUND_RETRY_COUNT attempts.
    """
    from decimal import Decimal

    from apps.payments.models import FailedRefund
    from domain.services.payment_services import (
        RefundPaymentService,
        _refunded_principal_total,
    )

    refundable_statuses = ['COLLECTED', 'ON_HOLD', 'DISBURSED', 'PARTIALLY_REFUNDED']
    with transaction.atomic():
        refund_ids = list(
            FailedRefund.objects.select_for_update(skip_locked=True)
            .filter(retry_count__lt=MAX_REFUND_RETRY_COUNT)
            .order_by('created_at')
            .values_list('id', flat=True)[:RETRY_BATCH_SIZE]
        )

    processed = 0
    for record in FailedRefund.objects.filter(id__in=refund_ids).select_related('payment'):
        payment = record.payment
        try:
            remaining = Decimal(str(payment.amount)) - _refunded_principal_total(payment)
            if payment.status not in refundable_statuses or remaining <= 0:
                # Already refunded through another path, or never collected.
                record.delete()
                continue

            payable_type, payable_id = payment.payable_type, payment.payable_id
            if not payable_type:
                # Legacy rows point at the payable through the FK only.
                if payment.order_id:
                    payable_type, payable_id = 'order', payment.order_id
                elif payment.pool_member_id:
                    payable_type, payable_id = 'pool_member', payment.pool_member_id
            if not payable_type or not payable_id:
                logger.error(f"Cannot resolve refund target for payment {payment.id}")
                record.delete()
                continue

            # Increment first so a crash mid-refund cannot retry forever.
            record.retry_count += 1
            record.save(update_fields=['retry_count'])

            refunded = RefundPaymentService.execute(
                payable_type,
                payable_id,
                override_amount=min(Decimal(str(record.amount)), remaining),
            )
            if refunded:
                record.delete()
                processed += 1
                logger.info(f"Retried refund for payment {payment.id} successfully")
        except Exception as e:
            logger.error(f"Retry failed for refund {record.id}: {str(e)}")
            record.reason = f"Last retry failed: {str(e)}"[:2000]
            record.save(update_fields=['reason'])

    if processed:
        logger.info(f"Processed {processed} refund retries")
    exhausted = FailedRefund.objects.filter(
        retry_count__gte=MAX_REFUND_RETRY_COUNT,
    ).count()
    if exhausted:
        logger.error(
            f"{exhausted} failed refunds exhausted retries; manual intervention needed"
        )
    return f"Retried {len(refund_ids)} failed refunds; {processed} completed."

@shared_task(name='apps.payments.tasks.cleanup_expired_payments')
def cleanup_expired_payments():
    """
    Cancel payments stuck in INITIATED or INPROCESS for more than 5 mins
    (USSD push timeout is ~120s, so 5min is a generous safety net).
    Before cancelling, poll the payment gateway status. If it was completed, recover it.
    """
    from datetime import timedelta

    from apps.payments.models import Payment
    from django.utils import timezone
    from domain.services.payment_services import CompletePaymentService
    from infrastructure.payment_gateways.factory import get_payment_gateway
    
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
