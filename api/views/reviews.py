from api.serializers.reviews import ReviewSerializer
from apps.reviews.models import Review
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response


class ReviewViewSet(viewsets.ModelViewSet):
    queryset = Review.objects.all()
    serializer_class = ReviewSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        queryset = Review.objects.all()
        restaurant_id = self.request.query_params.get('restaurant')
        driver_id = self.request.query_params.get('driver')

        if restaurant_id:
            queryset = queryset.filter(restaurant_id=restaurant_id)
        if driver_id:
            queryset = queryset.filter(driver_id=driver_id)

        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context['request'] = self.request
        return context

    def perform_create(self, serializer):
        serializer.save(customer=self.request.user)

    def perform_update(self, serializer):
        review = self.get_object()
        if review.customer != self.request.user:
            self.permission_denied(self.request, message="You can only edit your own reviews.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.customer != self.request.user:
            self.permission_denied(self.request, message="You can only delete your own reviews.")
        instance.delete()

    @action(detail=False, methods=['get'])
    def my(self, request):
        queryset = Review.objects.filter(customer=request.user)
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
