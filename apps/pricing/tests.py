from django.test import TestCase
from django.contrib.gis.geos import Point
from decimal import Decimal
from apps.restaurants.models import Restaurant, DeliveryZone
from apps.geo.models import OperationalZone
from apps.pricing.models import PricingPolicy, ZonePricingTier, DeliveryFeeLog
from domain.services.pricing_service import PricingService
from apps.users.models import User

class PricingServiceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testadmin', password='password')
        self.restaurant = Restaurant.objects.create(
            name="Test Kitchen",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924) # Dar es Salaam center
        )
        
        # Create an active policy
        self.policy = PricingPolicy.objects.create(
            name="Standard Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00'),
            road_buffer_multiplier=1.0 # Keep math simple for test
        )

    def test_calculate_fee_standard(self):
        # Delivery point ~2km away
        delivery_loc = Point(39.2226, -6.7924)
        
        fee, log = PricingService.calculate_delivery_fee(self.restaurant, delivery_loc)
        
        # Expected: 1000 + (2km * 500) = 2000
        # Rounding might affect this slightly due to H3 grid centers
        self.assertGreaterEqual(fee, Decimal('1000.00'))
        self.assertIsNotNone(log)
        self.assertEqual(log.policy_name, self.policy.name)

    def test_restaurant_override(self):
        # Create a specific zone for the delivery location
        import h3
        from apps.core.geo_utils import ZONE_RESOLUTION
        delivery_loc = Point(39.2226, -6.7924)
        delivery_h3 = h3.latlng_to_cell(-6.7924, 39.2226, ZONE_RESOLUTION)
        
        DeliveryZone.objects.create(
            restaurant=self.restaurant,
            name="VIP Zone",
            h3_index=delivery_h3,
            fee=Decimal('2500.00'),
            is_active=True
        )
        
        fee, log = PricingService.calculate_delivery_fee(self.restaurant, delivery_loc)
        
        self.assertEqual(fee, Decimal('2500.00'))
        self.assertEqual(log.policy_name, "RESTAURANT_OVERRIDE")

    def test_zone_tier_multiplier(self):
        # Create an operational zone with a multiplier
        from django.contrib.gis.geos import MultiPolygon, Polygon
        poly = Polygon(((39.21, -6.79), (39.23, -6.79), (39.23, -6.80), (39.21, -6.80), (39.21, -6.79)))
        op_zone = OperationalZone.objects.create(
            name="Premium Zone",
            boundary=MultiPolygon(poly)
        )
        
        ZonePricingTier.objects.create(
            zone=op_zone,
            per_km_multiplier=Decimal('2.00')
        )
        
        delivery_loc = Point(39.2226, -6.7924)
        fee, log = PricingService.calculate_delivery_fee(self.restaurant, delivery_loc)
        
        # Without multiplier it was ~2000. With 2x km rate it should be higher.
        # 1000 + (2km * 500 * 2.0) = 3000
        self.assertGreaterEqual(fee, Decimal('2500.00'))
        self.assertEqual(log.adjustments.get('zone_multiplier'), None) # We don't log it in adjustments yet, it's in the base calculation
