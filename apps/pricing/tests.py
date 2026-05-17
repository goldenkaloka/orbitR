from django.test import TestCase
from django.contrib.gis.geos import Point
from decimal import Decimal
from apps.restaurants.models import Restaurant
from apps.geo.models import OperationalZone
from apps.pricing.models import PricingPolicy, ZonePricingTier, DeliveryFeeLog, DistanceTier
from domain.services.pricing_service import PricingService
from apps.users.models import User

class PricingServiceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testadmin', password='password')
        self.restaurant = Restaurant.objects.create(
            name="Test Kitchen",
            owner=self.user,
            geo_location=Point(39.2026, -6.7924), # Dar es Salaam center
            max_delivery_radius_km=10.0
        )
        
        # Create an active policy
        self.policy = PricingPolicy.objects.create(
            name="Standard Policy",
            is_active=True,
            base_fee=Decimal('1000.00'),
            per_km_rate=Decimal('500.00')
        )

    def test_calculate_fee_standard(self):
        """Standard fee: base + distance * per_km_rate."""
        delivery_loc = Point(39.2226, -6.7924)  # ~2km away
        
        fee, s_fee, log = PricingService.calculate_delivery_fee(self.restaurant, delivery_loc)
        
        # Expected: 1000 + (2km * 500) = 2000 (approximately)
        self.assertGreaterEqual(fee, Decimal('1000.00'))
        self.assertIsNotNone(log)
        self.assertEqual(log.policy_name, self.policy.name)

    def test_distance_tier_escalation(self):
        """Distance tiers should increase the per-km rate for longer deliveries."""
        # Create tiers: 0-2km = 1.0x, 2-5km = 1.5x, 5+ = 2.0x
        DistanceTier.objects.create(policy=self.policy, min_km=0, max_km=2, multiplier=Decimal('1.00'))
        DistanceTier.objects.create(policy=self.policy, min_km=2, max_km=5, multiplier=Decimal('1.50'))
        DistanceTier.objects.create(policy=self.policy, min_km=5, max_km=999, multiplier=Decimal('2.00'))
        
        # Close delivery (~1km) — should use 1.0x tier
        close_loc = Point(39.2100, -6.7924)
        fee_close, _, _ = PricingService.calculate_delivery_fee(self.restaurant, close_loc)
        
        # Far delivery (~4km) — should use 1.5x tier
        far_loc = Point(39.2426, -6.7924)
        fee_far, _, _ = PricingService.calculate_delivery_fee(self.restaurant, far_loc)
        
        # Far should cost more than close
        self.assertGreater(fee_far, fee_close)

    def test_zone_tier_multiplier(self):
        """OperationalZone pricing tier multiplier should affect fees."""
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
        fee, s_fee, log = PricingService.calculate_delivery_fee(self.restaurant, delivery_loc)
        
        # With 2x multiplier the variable portion should be doubled
        # 1000 + (2km * 500 * 2.0) = 3000
        self.assertGreaterEqual(fee, Decimal('2500.00'))

    def test_no_tiers_defaults_to_1x(self):
        """Without distance tiers, multiplier should default to 1.0."""
        delivery_loc = Point(39.2226, -6.7924)
        
        fee, _, log = PricingService.calculate_delivery_fee(self.restaurant, delivery_loc)
        
        # Should not have a distance_tier adjustment
        self.assertNotIn('distance_tier', log.adjustments)

    def test_invalid_inputs_return_fallback(self):
        """Missing restaurant geo should return fallback fee."""
        restaurant_no_geo = Restaurant.objects.create(
            name="No Geo Kitchen",
            owner=self.user,
        )
        delivery_loc = Point(39.2226, -6.7924)
        
        fee, s_fee, log = PricingService.calculate_delivery_fee(restaurant_no_geo, delivery_loc)
        
        self.assertEqual(fee, Decimal('1000.00'))
        self.assertIsNone(log)
