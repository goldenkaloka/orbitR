import math
from decimal import Decimal
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
        """
        Primary strategy: Spatial query with weighted scoring and row-level locking.
        """
        from apps.delivery.models import Driver
        from django.contrib.gis.geos import Point
        from django.contrib.gis.db.models.functions import Distance
        from django.db.models import Count, Q, F
        from django.db import transaction
        from decimal import Decimal

        # Use a transaction to lock the potential drivers
        with transaction.atomic():
            import h3
            from apps.core.geo_utils import ZONE_RESOLUTION
            # 1. Calculate H3 cells for radius search
            restaurant_h3 = h3.latlng_to_cell(float(restaurant_lat), float(restaurant_lng), ZONE_RESOLUTION)
            
            # Approximate k-rings based on max_radius_km (res 9 is ~170m radius per cell)
            k_rings = int((max_radius_km * 1000) / 170)
            try:
                search_cells = list(h3.grid_disk(restaurant_h3, k_rings))
            except h3.H3ValueError:
                search_cells = [restaurant_h3]

            # 2. Filter available drivers within H3 cells
            # select_for_update(skip_locked=True) ensures we don't pick a driver
            # being processed by another dispatch task.
            drivers = Driver.objects.select_for_update(skip_locked=True).filter(
                status='AVAILABLE',
                h3_index__in=search_cells
            ).annotate(
                active_count=Count('orders', filter=Q(orders__status__in=['ASSIGNED', 'PICKED']))
            ).filter(
                active_count__lt=F('max_active_orders')
            )

            if not drivers.exists():
                return None

            # 3. Weighted Scoring (Load-balanced)
            # Factors: Distance (20%), Rating (20%), Load (60%)
            best_driver = None
            min_score = float('inf')

            for driver in drivers:
                # Avoid division by zero for rating
                rating_factor = 1.0 / float(max(driver.rating, Decimal('0.1')))
                
                # Approximate distance in km using h3.grid_distance
                try:
                    driver_dist_km = h3.grid_distance(restaurant_h3, driver.h3_index) * 0.170 if driver.h3_index else max_radius_km
                except h3.H3ValueError:
                    driver_dist_km = max_radius_km

                # Formula: Load-heavy to ensure busy drivers are deprioritized
                # Load (60%) weight ensures distance doesn't overwhelm capacity on campus scale
                score = (
                    float(driver_dist_km) * 0.2 +
                    rating_factor * 0.2 +
                    float(driver.active_count) * 0.6
                )
                
                if score < min_score:
                    min_score = score
                    best_driver = driver

            return best_driver

