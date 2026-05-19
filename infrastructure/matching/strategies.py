import math
from decimal import Decimal
from abc import ABC, abstractmethod
from django.contrib.gis.geos import Point
from django.contrib.gis.db.models.functions import Distance
from django.contrib.gis.measure import D
from django.db.models import Count, Q, F
from django.db import transaction
from apps.delivery.models import Driver



class LocationMatcher(ABC):
    @abstractmethod
    def find_best_driver(self, restaurant_location):
        pass

class ManualAssignmentStrategy(LocationMatcher):
    def find_best_driver(self, restaurant_location):
        # Simply returns None, relies on Logistics Manager to manually assign via API
        return None

class SimpleProximityStrategy(LocationMatcher):
    """
    MVP Proximity Matcher (Haversine Formula)
    Works with standard float latitude/longitude.
    """
    def find_best_driver(self, restaurant_lat, restaurant_lng, max_radius_km=10):
        from django.utils import timezone
        from datetime import timedelta
        threshold = timezone.now() - timedelta(minutes=5)
        available_drivers = Driver.objects.filter(
            status='AVAILABLE',
            last_heartbeat__gte=threshold
        )
        best_driver = None
        min_distance = float('inf')

        for driver in available_drivers:
            if driver.latitude is None or driver.longitude is None:
                continue
            
            distance = self._haversine(
                float(restaurant_lat), float(restaurant_lng), 
                float(driver.latitude), float(driver.longitude)
            )
            
            if distance < min_distance and distance <= max_radius_km:
                min_distance = distance
                best_driver = driver
        
        return best_driver

    def _haversine(self, lat1, lon1, lat2, lon2):
        R = 6371  # Earth radius in km
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) * math.sin(dlat / 2) +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) * math.sin(dlon / 2))
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

class PostgisRadiusStrategy(LocationMatcher):
    """
    High-performance spatial matching using PostGIS DWithin and weighted scoring.
    """
    def find_best_driver(self, restaurant_lat, restaurant_lng, max_radius_km=15, restaurant_id=None):
        """
        Primary strategy: Spatial query with weighted scoring and row-level locking.
        """
        from django.utils import timezone
        from datetime import timedelta
        threshold = timezone.now() - timedelta(minutes=5)
        restaurant_point = Point(float(restaurant_lng), float(restaurant_lat), srid=4326)

        with transaction.atomic():
            drivers = Driver.objects.select_for_update(skip_locked=True).filter(
                status='AVAILABLE',
                location__dwithin=(restaurant_point, D(km=max_radius_km)),
                last_heartbeat__gte=threshold
            ).filter(
                # Affinity Rule: If temp, must match restaurant. If permanent, any restaurant.
                Q(driver_type='permanent') | Q(assigned_by_id=restaurant_id)
            ).annotate(
                distance_meters=Distance('location', restaurant_point),
                active_count=Count('assignments', filter=Q(assignments__delivered_at__isnull=True))
            ).filter(
                active_count__lt=F('max_active_orders')
            )

            if not drivers.exists():
                return None

            # 2. Weighted Scoring (Load-balanced)
            # Factors: Distance (20%), Rating (20%), Load (60%)
            best_driver = None
            min_score = float('inf')

            for driver in drivers:
                # Rating Penalty: Higher rating = Lower penalty (0.0 penalty for 5.0 rating)
                rating_penalty = 5.0 - float(driver.rating)
                
                # Distance Factor (km)
                # distance_meters.m returns the value in meters from PostGIS geography
                dist_km = driver.distance_meters.km if driver.distance_meters else max_radius_km

                # Formula: Load-heavy to ensure busy drivers are deprioritized
                # score = (Dist * 0.2) + (RatingPenalty * 0.2) + (ActiveOrders * 0.6)
                # Goal: Minimize the score.
                score = (
                    float(dist_km) * 0.2 +
                    rating_penalty * 0.2 +
                    float(driver.active_count) * 0.6
                )
                
                if score < min_score:
                    min_score = score
                    best_driver = driver

            return best_driver

