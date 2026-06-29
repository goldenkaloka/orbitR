from decimal import Decimal
from datetime import timedelta
from unittest.mock import patch

from apps.marketplace.models import MarketplaceProduct
from apps.orders.models import OrderPool, PoolMember
from apps.payments.models import Payment
from apps.restaurants.models import Restaurant
from apps.users.models import User
from django.contrib.gis.geos import Point
from django.test import TestCase
from django.utils import timezone
from domain.services.payment_services import InitiatePaymentService, PaymentTargetResolver


class PaymentModelTests(TestCase):
    def test_payment_defaults_to_initiated(self):
        payment = Payment.objects.create(
            payable_type="marketplace_product",
            payable_id="listing-1",
            amount=Decimal("1000.00"),
        )

        self.assertEqual(payment.status, "INITIATED")

    def test_pool_member_payment_amount_includes_service_fee_share(self):
        user = User.objects.create_user(username="pool-user", phone="+255712000001")
        owner = User.objects.create_user(username="vendor", phone="+255712000002")
        restaurant = Restaurant.objects.create(
            owner=owner,
            name="Pool Restaurant",
            geo_location=Point(39.2, -6.8),
        )
        pool = OrderPool.objects.create(
            initiator=user,
            restaurant=restaurant,
            invite_code="POOL123",
            delivery_address="Campus",
            delivery_location=Point(39.21, -6.81),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        member = PoolMember.objects.create(
            pool=pool,
            user=user,
            items_subtotal=Decimal("5000.00"),
            delivery_fee_share=Decimal("1000.00"),
            service_fee_share=Decimal("300.00"),
        )

        target = PaymentTargetResolver.resolve("pool_member", member.id)

        self.assertEqual(target["amount"], Decimal("6300.00"))

    def test_retry_updates_unpaid_payment_amount_before_gateway_collect(self):
        seller = User.objects.create_user(username="seller", phone="+255712000003")
        product = MarketplaceProduct.objects.create(
            seller=seller,
            name="Laptop",
            description="Used laptop",
            price=Decimal("100000.00"),
            whatsapp_number="+255712000003",
        )
        Payment.objects.create(
            payable_type="marketplace_product",
            payable_id=str(product.id),
            amount=Decimal("1.00"),
            status="FAILED",
        )

        gateway = type("Gateway", (), {
            "collect": lambda self, **kwargs: {
                "success": True,
                "provider_reference": "ref-1",
                "collected_amount": kwargs["amount"],
            }
        })()

        with patch("domain.services.payment_services.get_payment_gateway", return_value=gateway):
            result = InitiatePaymentService.execute(
                "marketplace_product",
                product.id,
                actor=seller,
            )

        payment = result["payment"]
        self.assertEqual(payment.amount, Decimal("200.00"))
        self.assertEqual(result["gateway_response"]["collected_amount"], Decimal("200.00"))
