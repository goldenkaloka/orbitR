"""API for scheduled drop points, windows and the customer calendar."""

import logging

from api.serializers.drop import DropPointSerializer, DropWindowSerializer
from api.views.admin import IsAdmin
from apps.orders.models import DropPoint, DropWindow
from domain.services.orders.drop_service import nearest_points, open_windows
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


def _int_param(request, name, default):
    try:
        return max(1, min(int(request.query_params.get(name, default)), 14))
    except (TypeError, ValueError):
        return default


def _float_param(request, *names):
    for name in names:
        raw = request.query_params.get(name)
        if raw in (None, ''):
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


class DropPointViewSet(viewsets.ModelViewSet):
    """Drop points: readable by any signed-in user, editable by admins."""

    queryset = DropPoint.objects.select_related('zone').prefetch_related('windows')
    serializer_class = DropPointSerializer

    def get_permissions(self):
        if self.action in ('list', 'retrieve', 'calendar'):
            return [IsAuthenticated()]
        return [IsAuthenticated(), IsAdmin()]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        if not (user.is_authenticated and user.is_staff):
            queryset = queryset.filter(is_active=True)
        return queryset

    @action(detail=True, methods=['GET'])
    def calendar(self, request, pk=None):
        """Bookable occurrences of this point's windows."""
        days = _int_param(request, 'days', 2)
        point = self.get_object()
        occurrences = open_windows(point=point, days=days)
        return Response({'count': len(occurrences), 'results': occurrences})


class DropWindowViewSet(viewsets.ModelViewSet):
    """Window CRUD for the admin console."""

    queryset = DropWindow.objects.select_related('drop_point').order_by(
        'drop_point__name', 'start_time',
    )
    serializer_class = DropWindowSerializer
    permission_classes = [IsAuthenticated, IsAdmin]

    def get_queryset(self):
        queryset = super().get_queryset()
        point = self.request.query_params.get('drop_point')
        if point:
            queryset = queryset.filter(drop_point_id=point)
        return queryset


class DropCalendarView(APIView):
    """Every bookable drop occurrence near a coordinate.

    ``GET /api/drop-calendar/?lat=&lng=&days=2`` returns the windows the
    checkout screen should offer, soonest first. Filtering by proximity keeps a
    customer in Kijichi from being offered a drop in Mikocheni.

    Public on purpose: the home feed advertises shared drops before sign-in, so
    a guest can see that a rider is already going to their gate. It exposes a
    schedule, not anyone's order or address.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        days = _int_param(request, 'days', 2)
        latitude = _float_param(request, 'latitude', 'lat')
        longitude = _float_param(request, 'longitude', 'lng', 'lon')

        point = None
        point_id = request.query_params.get('drop_point')
        if point_id:
            point = DropPoint.objects.filter(id=point_id, is_active=True).first()
            if point is None:
                return Response(
                    {'detail': 'This drop point is not available.'},
                    status=status.HTTP_404_NOT_FOUND,
                )

        occurrences = open_windows(point=point, days=days)

        if point is None and latitude is not None and longitude is not None:
            nearby = {str(p.id) for p in nearest_points(latitude, longitude, limit=20)}
            occurrences = [
                item for item in occurrences
                if item['drop_point_id'] in nearby
            ]

        return Response({'count': len(occurrences), 'results': occurrences})
