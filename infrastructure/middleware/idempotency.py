import json
import logging

from apps.core.models import IdempotencyKey
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)


class IdempotencyMiddleware(MiddlewareMixin):
    """
    Replay a previously-handled response for a repeated ``X-Idempotency-Key``.

    Scoping is by key alone (the client is expected to generate a unique UUID
    per logical operation, and ``IdempotencyKey.key`` is globally unique). We
    deliberately do NOT scope by ``request.user``: at middleware time JWT is
    not resolved yet (DRF authenticates inside the view), so ``request.user``
    is usually ``AnonymousUser`` and a user-scoped lookup would both mismatch
    on create and could collide on the unique key.
    """

    def process_request(self, request):
        if request.method not in ['POST', 'PATCH', 'PUT']:
            return None

        idempotency_key = request.headers.get('X-Idempotency-Key')
        if not idempotency_key:
            return None

        existing = IdempotencyKey.objects.filter(key=idempotency_key).first()
        if existing:
            return JsonResponse(
                data=existing.response_body,
                status=existing.response_code,
            )

        request._idempotency_key = idempotency_key
        return None

    def process_response(self, request, response):
        idempotency_key = getattr(request, '_idempotency_key', None)
        if not idempotency_key:
            return response

        # Cache only successful responses. Caching 4xx would freeze transient
        # validation/conflict errors permanently for that key.
        if 200 <= response.status_code < 300:
            try:
                if hasattr(response, 'data'):  # DRF Response
                    content = response.data
                else:
                    content = json.loads(response.content.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning(f"Failed to parse response body for idempotency key: {e}")
                content = {}
            except Exception as e:
                logger.error(f"Unexpected error in idempotency middleware: {e}")
                content = {}

            user = request.user if getattr(request, 'user', None) and request.user.is_authenticated else None

            IdempotencyKey.objects.get_or_create(
                key=idempotency_key,
                defaults={
                    'user': user,
                    'response_code': response.status_code,
                    'response_body': content,
                },
            )

        return response
