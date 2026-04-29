from rest_framework import viewsets, permissions
from apps.reviews.models import Review
from api.serializers.reviews import ReviewSerializer

class ReviewViewSet(viewsets.ModelViewSet):
    queryset = Review.objects.all()
    serializer_class = ReviewSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Allow filtering by restaurant or driver
        queryset = Review.objects.all()
        restaurant_id = self.request.query_params.get('restaurant')
        driver_id = self.request.query_params.get('driver')
        
        if restaurant_id:
            queryset = queryset.filter(restaurant_id=restaurant_id)
        if driver_id:
            queryset = queryset.filter(driver_id=driver_id)
            
        return queryset

    def perform_create(self, serializer):
        serializer.save(customer=self.request.user)
