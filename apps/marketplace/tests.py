from decimal import Decimal

from apps.marketplace.models import MarketplaceCategory, MarketplaceProduct
from apps.users.models import User
from django.core.exceptions import ValidationError
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
            condition="USED",
            whatsapp_number="+255670000001",
        )

        self.assertEqual(product.calculate_listing_fee(), Decimal("100.00"))

    def test_only_three_conditions_are_accepted(self):
        self.assertEqual(
            [value for value, _ in MarketplaceProduct.CONDITION_CHOICES],
            ['NEW', 'USED', 'OTHER'],
        )

        seller = User.objects.create_user(
            phone="+255670000002", username="seller2", password="pass",
        )
        product = MarketplaceProduct(
            seller=seller,
            name="Old radio",
            description="Works",
            price=Decimal("10000.00"),
            condition="LIKE_NEW",
            whatsapp_number="+255670000002",
        )
        with self.assertRaises(ValidationError):
            product.full_clean()
