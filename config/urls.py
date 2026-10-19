from apps.geo.views import ZonesGeoJSON
from apps.users.views import DeleteAccountView
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.db import connections
from django.http import JsonResponse
from django.urls import include, path
from django.views.decorators.http import require_GET
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)


@require_GET
def health_check(request):
    """Health-check endpoint for load balancer probes."""
    db_ok = True
    try:
        connections['default'].cursor().execute('SELECT 1')
    except Exception:
        db_ok = False

    status = 200 if db_ok else 503
    return JsonResponse({
        'status': 'ok' if db_ok else 'degraded',
        'database': 'connected' if db_ok else 'unreachable',
        'debug': settings.DEBUG,
    }, status=status)


urlpatterns = [
    path('health/', health_check, name='health_check'),
    path('admin/', admin.site.urls),
    path('auth/login/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('api/', include('api.routers')),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/schema/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    path('geo/zones.geojson', ZonesGeoJSON.as_view(), name='zones_geojson'),
    path('delete-account/', DeleteAccountView.as_view(), name='delete_account'),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    # Performance profiler dashboard (staff-only, dev only).
    try:
        import silk
        urlpatterns += [path('silk/', include('silk.urls', namespace='silk'))]
    except ImportError:
        pass
