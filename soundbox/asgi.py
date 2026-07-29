import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'soundbox.settings')

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

# Initialize Django before importing application routing/consumers.
django_asgi_application = get_asgi_application()

from api.routing import websocket_urlpatterns
from api.websocket_auth import JWTAuthMiddleware

application = ProtocolTypeRouter({
    'http': django_asgi_application,
    'websocket': AllowedHostsOriginValidator(
        JWTAuthMiddleware(URLRouter(websocket_urlpatterns))
    ),
})
