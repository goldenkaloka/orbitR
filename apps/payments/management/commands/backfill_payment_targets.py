from django.core.management.base import BaseCommand
from django.db import transaction
from apps.payments.models import Payment

class Command(BaseCommand):
    help = 'Backfill payable_type and payable_id for existing Payment records'

    def handle(self, *args, **options):
        payments = Payment.objects.filter(payable_id__isnull=True)
        count = payments.count()
        self.stdout.write(f"Starting backfill for {count} payments...")

        with transaction.atomic():
            for payment in payments:
                if payment.order:
                    payment.payable_type = 'order'
                    payment.payable_id = payment.order.id
                elif payment.pool_member:
                    payment.payable_type = 'pool_member'
                    payment.payable_id = payment.pool_member.id
                elif payment.marketplace_product:
                    payment.payable_type = 'marketplace_product'
                    payment.payable_id = payment.marketplace_product.id
                
                if payment.payable_id:
                    payment.save(update_fields=['payable_type', 'payable_id'])

        self.stdout.write(self.style.SUCCESS(f"Successfully backfilled {count} payments."))
