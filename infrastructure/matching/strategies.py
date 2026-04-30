import math
from abc import ABC, abstractmethod
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
        available_drivers = Driver.objects.filter(status='AVAILABLE')
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
    High-performance spatial matching using PostGIS DWithin.
    """
    def find_best_driver(self, restaurant_lat, restaurant_lng, max_radius_km=15):
        from django.contrib.gis.geos import Point
        from django.contrib.gis.db.models.functions import Distance
        from django.db.models import Count, Q, F
        
        # 1. Create spatial point
        pnt = Point(float(restaurant_lng), float(restaurant_lat), srid=4326)
        
        # 2. Filter using spatial index
        # We search within distance (in meters) and check if driver is under their capacity limit
        drivers = Driver.objects.filter(
            status='AVAILABLE',
            location__dwithin=(pnt, max_radius_km * 1000)
        ).annotate(
            distance=Distance('location', pnt),
            active_count=Count('orders', filter=Q(orders__status__in=['ASSIGNED', 'PICKED', 'PICKED_UP']))
        ).filter(
            active_count__lt=F('max_active_orders')
        )
        
        if not drivers.exists():
            return None

        # 3. Calculate weighted score (Lower is better)
        # Factors: Distance (40%), Rating (30%), Load (30%)
        # Score = (dist_km * 0.4) + (1/rating * 0.3) + (active_count * 0.3)
        scored_drivers = []
        for driver in drivers:
            # Avoid division by zero for rating
            rating_factor = 1.0 / float(max(driver.rating, Decimal('0.1')))
            
            score = (
                float(driver.distance.km) * 0.4 +
                rating_factor * 0.3 +
                float(driver.active_count) * 0.3
            )
            scored_drivers.append((score, driver))
            
        # Sort by score ascending
        scored_drivers.sort(key=lambda x: x[0])
        
        return scored_drivers[0][1]

