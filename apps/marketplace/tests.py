from decimal import Decimal

from apps.marketplace.models import MarketplaceCategory, MarketplaceProduct
from apps.users.models import User
from django.test import TestCase


class MarketplaceProductTests(TestCase):
    def test_listing_fee_is_point_two_percent_of_price(self):
        seller = User.objects.create_user(
            phone="+255670000001",
            username="seller",
            password="pass",
        )
        category = MarketplaceCategory.objects.create(name="Books")
        product = MarketplaceProduct.objects.create(
            seller=seller,
            category=category,
            name="Calculus Notes",
            description="Clean lecture notes",
            price=Decimal("50000.00"),
            condition="GOOD",
            whatsapp_number="+255670000001",
        )

        self.assertEqual(product.calculate_listing_fee(), Decimal("100.00"))
