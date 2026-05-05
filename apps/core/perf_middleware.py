import logging
import time

from django.conf import settings

logger = logging.getLogger("perf.request")


class RequestTimingMiddleware:
    """
    Lightweight request timing middleware for p95 monitoring baselines.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self.threshold_ms = int(getattr(settings, "PERF_SLOW_REQUEST_MS", 700))

    def __call__(self, request):
        start = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - start) * 1000

        if elapsed_ms >= self.threshold_ms:
            logger.warning(
                "slow_request method=%s path=%s status=%s duration_ms=%.2f",
                request.method,
                request.path,
                getattr(response, "status_code", "unknown"),
                elapsed_ms,
            )
        return response

