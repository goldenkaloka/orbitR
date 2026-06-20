import json
import logging

from apps.core.models import IdempotencyKey
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)

class IdempotencyMiddleware(MiddlewareMixin):
    def process_request(self, request):
        if request.method not in ['POST', 'PATCH', 'PUT']:
            return None
        
        idempotency_key = request.headers.get('X-Idempotency-Key')
        if not idempotency_key:
            return None
        
        # Check if we already processed this key
        user = request.user if request.user.is_authenticated else None
        existing = IdempotencyKey.objects.filter(key=idempotency_key, user=user).first()
        
        if existing:
            return JsonResponse(
                data=existing.response_body,
                status=existing.response_code
            )
        
        request._idempotency_key = idempotency_key
        return None

    def process_response(self, request, response):
        idempotency_key = getattr(request, '_idempotency_key', None)
        if not idempotency_key:
            return response

        # Only cache successful or specific client error responses to avoid permanent caching of transient server errors
        if 200 <= response.status_code < 500:
            user = request.user if request.user.is_authenticated else None
            
            try:
                # Attempt to parse response body if it's JSON
                if hasattr(response, 'data'): # DRF Response
                    content = response.data
                else:
                    content = json.loads(response.content.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                logger.warning(f"Failed to parse response body for idempotency key: {e}")
                content = {}
            except Exception as e:
                logger.error(f"Unexpected error in idempotency middleware: {e}")
                content = {}

            IdempotencyKey.objects.get_or_create(
                key=idempotency_key,
                user=user,
                defaults={
                    'response_code': response.status_code,
                    'response_body': content
                }
            )
            
        return response
