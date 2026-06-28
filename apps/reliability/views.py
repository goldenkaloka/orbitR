from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ReliabilityRecord
from .serializers import ReliabilitySerializer
from .services import get_reliability


class ReliabilityViewSet(viewsets.GenericViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = ReliabilitySerializer

    def get_queryset(self):
        return ReliabilityRecord.objects.filter(user=self.request.user).prefetch_related('strikes')

    @action(detail=False, methods=['GET'])
    def me(self, request):
        role = request.query_params.get('role', 'customer')
        record = get_reliability(request.user, role)
        record.refresh_from_db()
        serializer = self.get_serializer(record)
        return Response(serializer.data)
