from datetime import timedelta

from apps.geo.place_aliases import (
    record_address_place_alias,
    record_order_place_alias,
)
from apps.orders.models import Order
from apps.users.models import UserAddress
from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = (
        'Backfill crowd-sourced place aliases from saved addresses and '
        'delivered orders.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=180,
            help='How far back to scan delivered orders (default 180).',
        )
        parser.add_argument(
            '--addresses-only', action='store_true', help='Skip delivered orders.'
        )
        parser.add_argument(
            '--orders-only', action='store_true', help='Skip saved addresses.'
        )

    def handle(self, *args, **options):
        created = 0
        if not options['orders_only']:
            for address in UserAddress.objects.select_related('zone').iterator():
                if record_address_place_alias(address) is not None:
                    created += 1
            self.stdout.write(f'Addresses processed: {created}')

        if not options['addresses_only']:
            cutoff = timezone.now() - timedelta(days=options['days'])
            orders = Order.objects.filter(
                status='DELIVERED',
                fulfillment_type='DELIVERY',
                created_at__gte=cutoff,
            ).iterator()
            order_count = 0
            for order in orders:
                if record_order_place_alias(order) is not None:
                    order_count += 1
            self.stdout.write(f'Orders processed: {order_count}')

        self.stdout.write(self.style.SUCCESS('Place alias backfill complete.'))
