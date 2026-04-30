"""
ASGI config for mwanachuo project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.0/howto/deployment/asgi/
"""

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.core.asgi import get_asgi_application
django_asgi_app = get_asgi_application()

from apps.events.middleware import JWTAuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
import apps.events.routing

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": JWTAuthMiddlewareStack(
        URLRouter(
            apps.events.routing.websocket_urlpatterns
        )
    ),
})
