"""Signed downloads for pickup counter receipts.

The receipt is rendered from the order on demand, so the signed token is the
only state a browser needs. This mirrors the pre-order sheet download: the
system browser cannot carry the app's bearer token, so the link carries its
own short-lived authorization.
"""

import io

from apps.orders.models import Order
from django.core import signing
from django.http import FileResponse
from domain.services.orders.pickup_receipt import (
    build_pickup_receipt_pdf,
    receipt_filename,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

DOWNLOAD_SALT = 'pickup-receipt-download'
# Long enough to open from the app, short enough that a URL pasted into a
# chat stops working the same day.
DOWNLOAD_MAX_AGE = 900


def build_receipt_download_token(order_id):
    return signing.dumps({'order': str(order_id)}, salt=DOWNLOAD_SALT)


class PickupReceiptDownloadView(APIView):
    """Serves one order's receipt to a browser holding a signed link."""

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
                {'error': 'This receipt link has expired. Open the receipt in the app again.'},
                status=403,
            )

        if payload.get('order') != str(pk):
            return Response({'error': 'Invalid receipt link.'}, status=403)

        order = (
            Order.objects.select_related(
                'restaurant', 'restaurant__owner', 'customer',
            )
            .prefetch_related('items__food')
            .filter(id=pk)
            .first()
        )
        if order is None:
            return Response({'error': 'Order not found.'}, status=404)
        if order.fulfillment_type != Order.FULFILLMENT_PICKUP:
            return Response(
                {'error': 'Only pickup orders have a counter receipt.'},
                status=404,
            )

        response = FileResponse(
            io.BytesIO(build_pickup_receipt_pdf(order)),
            content_type='application/pdf',
        )
        response['Content-Disposition'] = (
            f'attachment; filename="{receipt_filename(order)}"'
        )
        return response
