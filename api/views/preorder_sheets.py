"""Vendor access to the frozen pre-order kitchen sheets."""

import io
import logging
from urllib.parse import quote

from api.serializers.core import PreorderSheetSerializer
from apps.orders.models import PreorderSheet
from apps.restaurants.models import Restaurant, SectionStaff
from django.core import signing
from django.http import FileResponse
from django.urls import reverse
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

DOWNLOAD_SALT = 'preorder-sheet-download'
# Long enough to open a link from the app, short enough that a URL pasted into
# a chat stops working the same day.
DOWNLOAD_MAX_AGE = 900


def build_download_token(sheet_id):
    return signing.dumps({'sheet': str(sheet_id)}, salt=DOWNLOAD_SALT)


class PreorderSheetViewSet(viewsets.ReadOnlyModelViewSet):
    """The sheets belonging to the caller's restaurant.

    Admins may scope with ``?restaurant=<id>``; vendor owners and stall staff
    only ever see the restaurants they belong to, enforced in the queryset so
    a foreign sheet id 404s instead of leaking.
    """

    serializer_class = PreorderSheetSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = PreorderSheet.objects.select_related('restaurant').order_by(
            '-service_date', 'locked_at',
        )

        if getattr(user, 'role', None) == 'admin':
            restaurant_id = self.request.query_params.get('restaurant')
            return (
                queryset.filter(restaurant_id=restaurant_id)
                if restaurant_id else queryset
            )

        permitted = set(
            Restaurant.objects.filter(owner=user).values_list('id', flat=True)
        )
        permitted |= set(
            SectionStaff.objects.filter(user=user)
            .values_list('section__restaurant_id', flat=True)
        )

        restaurant_id = self.request.query_params.get('restaurant')
        if restaurant_id:
            permitted = {rid for rid in permitted if str(rid) == restaurant_id}
        return queryset.filter(restaurant_id__in=permitted)

    @action(detail=True, methods=['GET'], url_path='download-link')
    def download_link(self, request, pk=None):
        """A short-lived, unauthenticated URL the browser can download from."""
        sheet = self.get_object()
        target = request.build_absolute_uri(
            reverse('preorder-sheet-download', args=[sheet.id])
        )
        token = quote(build_download_token(sheet.id), safe='')
        return Response({
            'url': f'{target}?token={token}',
            'filename': sheet.filename,
            'expires_in': DOWNLOAD_MAX_AGE,
        })


class PreorderSheetDownloadView(APIView):
    """Serves the PDF to a browser holding a signed link.

    Phones download through the system browser, which cannot attach the app's
    bearer token, so the signed token carries the authorization instead.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, pk):
        token = request.query_params.get('token', '')
        try:
            payload = signing.loads(
                token, salt=DOWNLOAD_SALT, max_age=DOWNLOAD_MAX_AGE,
            )
        except signing.BadSignature:
            return Response(
                {'error': 'This download link has expired. Reopen the sheet in the app.'},
                status=403,
            )

        if payload.get('sheet') != str(pk):
            return Response({'error': 'Invalid download link.'}, status=403)

        sheet = PreorderSheet.objects.filter(id=pk).first()
        if sheet is None:
            return Response({'error': 'Sheet not found.'}, status=404)
        if not sheet.pdf:
            logger.error('[PreorderSheet] Missing PDF bytes for sheet %s', pk)
            return Response({'error': 'Sheet file is missing.'}, status=404)

        response = FileResponse(
            io.BytesIO(bytes(sheet.pdf)), content_type='application/pdf',
        )
        response['Content-Disposition'] = (
            f'attachment; filename="{sheet.filename}"'
        )
        return response
