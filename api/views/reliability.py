from apps.reliability.serializers import ReliabilitySerializer
from apps.reliability.services import get_reliability
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


class ReliabilityView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        role = request.query_params.get('role', 'customer')
        record = get_reliability(request.user, role)
        record.refresh_from_db()
        serializer = ReliabilitySerializer(record)
        return Response(serializer.data)
