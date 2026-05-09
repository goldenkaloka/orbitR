from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from api.serializers.core import DriverSerializer, DeliveryProofSerializer
from apps.delivery.models import Driver, DeliveryProof

from domain.fsm.driver_fsm import DriverFSM
from apps.core.geo_utils import ZONE_RESOLUTION, latlng_to_h3
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.utils import timezone
from apps.orders.models import Order
from infrastructure.notifications.service import NotificationService
import math

class DriverViewSet(viewsets.ModelViewSet):
    queryset = Driver.objects.all()
    serializer_class = DriverSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if self.request.user.role == 'driver':
            return Driver.objects.filter(user=self.request.user)
        return super().get_queryset()

    @action(detail=False, methods=['POST'])
    def online(self, request):
        driver = request.user.driver_profile
        try:
            driver.status = DriverFSM.transition(driver.status, 'AVAILABLE')
            driver.save()
            # Invalidate stats cache
            cache.delete(f"stats_{request.user.id}")
            return Response(DriverSerializer(driver).data)
        except Exception as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=False, methods=['POST'])
    def offline(self, request):
        driver = request.user.driver_profile
        try:
            driver.status = DriverFSM.transition(driver.status, 'OFFLINE')
            driver.save()
            # Invalidate stats cache
            cache.delete(f"stats_{request.user.id}")
            return Response(DriverSerializer(driver).data)
        except Exception as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=False, methods=['POST'], url_path='location')
    def update_location(self, request):
        lat = request.data.get('lat')
        lng = request.data.get('lng')
        if lat is None or lng is None:
            return Response({"error": "lat and lng are required"}, status=400)

        # Validate numeric + finite + range to avoid 500s and bad geospatial writes
        try:
            lat_f = float(lat)
            lng_f = float(lng)
        except (TypeError, ValueError):
            return Response({"error": "Invalid lat or lng"}, status=400)
        if not (math.isfinite(lat_f) and math.isfinite(lng_f)):
            return Response({"error": "Invalid lat or lng"}, status=400)
        if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
            return Response({"error": "lat/lng out of range"}, status=400)
        
        driver = request.user.driver_profile
        
        # 1. Throttling: 1 update per 10 seconds per driver
        cache_key = f"driver_loc_throttle_{driver.id}"
        if cache.get(cache_key):
            return Response({'status': 'throttled', 'message': 'Location updates too frequent'}, status=200)
        
        # 2. Status Check: Only active/busy drivers broadcast location
        if driver.status not in ['AVAILABLE', 'DELIVERING']:
             return Response({'status': 'ignored', 'message': 'Driver is offline or inactive'}, status=200)

        new_loc = Point(lng_f, lat_f, srid=4326)
        
        # 3. Distance Threshold: Only save if moved > 50 meters
        if driver.location:
            # PostGIS geography.distance() returns meters
            dist = driver.location.distance(new_loc)
            if dist < 50:
                # Still set cache to throttle even if we don't save to DB
                cache.set(cache_key, True, 10)
                return Response({'status': 'skipped', 'message': 'Movement below threshold'}, status=200)

        # 4. Persistence and Sync
        driver.location = new_loc
        driver.latitude = lat_f
        driver.longitude = lng_f
        driver.h3_index = latlng_to_h3(lat_f, lng_f, ZONE_RESOLUTION)
        driver.save(update_fields=['location', 'latitude', 'longitude', 'h3_index', 'updated_at'])
        
        cache.set(cache_key, True, 10)
        
        # --- Arrival Notifications Logic ---
        active_orders = Order.objects.filter(driver=driver, status='PICKED')
        for order in active_orders:
            if order.delivery_location:
                # Fixed variable name from new_location to new_loc
                dist_to_customer = new_loc.distance(order.delivery_location)
                if dist_to_customer < 300: # 300 meters
                    # Check cache to avoid duplicate notifications within 10 mins
                    notif_cache_key = f"nearby_notif_{order.id}"
                    if not cache.get(notif_cache_key):
                        NotificationService.send_push(
                            user_id=str(order.customer.id),
                            title="Rider Nearby! 🛵",
                            body="Your rider is almost at your location. Please get ready!",
                            data={"order_id": str(order.id), "type": "rider_nearby"}
                        )
                        cache.set(notif_cache_key, True, 600)
        
        return Response({'status': 'location updated'})

class DeliveryProofViewSet(viewsets.ModelViewSet):
    queryset = DeliveryProof.objects.all()
    serializer_class = DeliveryProofSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        # Ensure only the assigned driver can submit proof
        order = serializer.validated_data['order']
        driver = self.request.user.driver_profile
        if order.driver != driver:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You are not the driver assigned to this order.")
        serializer.save(delivered_by=driver)
