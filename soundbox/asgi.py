import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'soundbox.settings')

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import OriginValidator
from django.conf import settings
from django.core.asgi import get_asgi_application

# Initialize Django before importing application routing/consumers.
django_asgi_application = get_asgi_application()

from api.routing import websocket_urlpatterns
from api.websocket_auth import JWTAuthMiddleware

application = ProtocolTypeRouter({
    'http': django_asgi_application,
    'websocket': OriginValidator(
        JWTAuthMiddleware(URLRouter(websocket_urlpatterns)),
        settings.WEBSOCKET_ALLOWED_ORIGINS,
    ),
})
