from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from api.serializers.core import DriverSerializer, DeliveryProofSerializer
from apps.delivery.models import Driver, DeliveryProof

from domain.fsm.driver_fsm import DriverFSM

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
            return Response(DriverSerializer(driver).data)
        except Exception as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=False, methods=['POST'])
    def offline(self, request):
        driver = request.user.driver_profile
        try:
            driver.status = DriverFSM.transition(driver.status, 'OFFLINE')
            driver.save()
            return Response(DriverSerializer(driver).data)
        except Exception as e:
            return Response({"error": str(e)}, status=400)

    @action(detail=False, methods=['POST'], url_path='location')
    def update_location(self, request):
        lat = request.data.get('lat')
        lng = request.data.get('lng')
        if lat is None or lng is None:
            return Response({"error": "lat and lng are required"}, status=400)
        
        driver = request.user.driver_profile
        driver.latitude = lat
        driver.longitude = lng
        
        # Update PostGIS point
        from django.contrib.gis.geos import Point
        new_location = Point(float(lng), float(lat), srid=4326)
        driver.location = new_location
        driver.save()
        
        # --- Arrival Notifications Logic ---
        # Find active orders in transit for this driver
        from apps.orders.models import Order
        from infrastructure.notifications.service import NotificationService
        from django.core.cache import cache
        
        active_orders = Order.objects.filter(driver=driver, status='PICKED_UP')
        for order in active_orders:
            if order.delivery_location:
                # Calculate distance in meters (geography=True)
                distance = new_location.distance(order.delivery_location)
                if distance < 300: # 300 meters
                    # Check cache to avoid duplicate notifications within 10 mins
                    cache_key = f"nearby_notif_{order.id}"
                    if not cache.get(cache_key):
                        NotificationService.send_push(
                            user_id=str(order.customer.id),
                            title="Rider Nearby! 🛵",
                            body="Your rider is almost at your location. Please get ready!",
                            data={"order_id": str(order.id), "type": "rider_nearby"}
                        )
                        cache.set(cache_key, True, 600)
        
        return Response(DriverSerializer(driver).data)

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
