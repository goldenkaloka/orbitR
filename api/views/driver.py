import math

from api.serializers.core import DeliveryProofSerializer, DriverSerializer
from apps.core.geo_utils import ZONE_RESOLUTION, haversine_distance, latlng_to_h3
from apps.delivery.models import DeliveryProof, Driver
from apps.orders.models import Order
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.utils import timezone
from domain.fsm.driver_fsm import DriverFSM
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response


class IsDriver(BasePermission):
    message = "User has no driver profile"

    def has_permission(self, request, view):
        return hasattr(request.user, 'driver_profile')

class DriverViewSet(viewsets.ModelViewSet):
    queryset = Driver.objects.all()
    serializer_class = DriverSerializer
    permission_classes = [IsAuthenticated, IsDriver]

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
            cache.delete(f"user_stats_{request.user.id}_{request.user.role}")

            # Notify Centrifugo so the app and admin dashboard update in real-time
            from apps.events.tasks import centrifugo_publish
            centrifugo_publish.delay(f'riders:{request.user.id}', {
                'type': 'driver_status_change',
                'driver_id': str(driver.id),
                'status': driver.status,
            })
            centrifugo_publish.delay('riders_global:pool', {
                'type': 'driver_availability',
                'driver_id': str(driver.id),
                'status': driver.status,
                'action': 'refresh_driver_pool',
            })

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
            cache.delete(f"user_stats_{request.user.id}_{request.user.role}")

            # Notify Centrifugo so the app and admin dashboard update in real-time
            from apps.events.tasks import centrifugo_publish
            centrifugo_publish.delay(f'riders:{request.user.id}', {
                'type': 'driver_status_change',
                'driver_id': str(driver.id),
                'status': driver.status,
            })
            centrifugo_publish.delay('riders_global:pool', {
                'type': 'driver_availability',
                'driver_id': str(driver.id),
                'status': driver.status,
                'action': 'refresh_driver_pool',
            })

            return Response(DriverSerializer(driver).data)
        except Exception as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=False, methods=['POST'], url_path='location')
    def update_location(self, request):
        lat = request.data.get('lat')
        lng = request.data.get('lng')
        if lat is None or lng is None:
            return Response({"error": "lat and lng are required"}, status=400)

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

        cache_key = f"driver_loc_throttle_{driver.id}"
        if cache.get(cache_key):
            return Response({'status': 'throttled', 'message': 'Location updates too frequent'}, status=200)

        if driver.status not in ['AVAILABLE', 'DELIVERING']:
             return Response({'status': 'ignored', 'message': 'Driver is offline or inactive'}, status=200)

        new_loc = Point(lng_f, lat_f, srid=4326)

        if driver.location:
            dist = haversine_distance(driver.location, new_loc)
            if dist < 50:
                driver.last_heartbeat = timezone.now()
                driver.save(update_fields=['last_heartbeat'])
                cache.set(cache_key, True, 10)
                return Response({'status': 'skipped', 'message': 'Movement below threshold'}, status=200)

        driver.location = new_loc
        driver.h3_index = latlng_to_h3(lat_f, lng_f, ZONE_RESOLUTION)
        driver.last_heartbeat = timezone.now()
        driver.save(update_fields=['location', 'h3_index', 'last_heartbeat'])


        cache.set(cache_key, True, 10)

        from apps.delivery.tasks import broadcast_driver_location
        active_orders = Order.objects.filter(driver=driver, status__in=['ASSIGNED', 'PICKED'])
        for order in active_orders:
            broadcast_driver_location.delay(
                order_id=str(order.id),
                lat=lat_f,
                lng=lng_f,
                heading=request.data.get('heading', 0.0),
            )

        return Response({'status': 'location updated'})

    @action(detail=False, methods=['POST'], url_path='heartbeat')
    def heartbeat(self, request):
        driver = request.user.driver_profile
        driver.last_heartbeat = timezone.now()
        driver.save(update_fields=['last_heartbeat'])
        return Response({'status': 'ok', 'last_heartbeat': driver.last_heartbeat})

class DeliveryProofViewSet(viewsets.ModelViewSet):
    queryset = DeliveryProof.objects.all()
    serializer_class = DeliveryProofSerializer
    permission_classes = [IsAuthenticated, IsDriver]

    def perform_create(self, serializer):
        # Ensure only the assigned driver can submit proof
        order = serializer.validated_data['order']
        driver = self.request.user.driver_profile
        if order.driver != driver:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You are not the driver assigned to this order.")
        serializer.save(delivered_by=driver)
