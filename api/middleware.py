from django.conf import settings
from django.http import HttpResponseBadRequest

from .utils import MediaPipelineError, sign_r2_urls_in_payload


class RejectProxyConnectMiddleware:
    """Reject public open-proxy probes before Django evaluates their fake Host."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method.upper() == 'CONNECT':
            return HttpResponseBadRequest('Unsupported request method.')
        return self.get_response(request)


def _sign_response(response, *, expiration, refresh=False, strict=True):
    if not hasattr(response, 'data'):
        return response
    try:
        response.data = sign_r2_urls_in_payload(
            response.data,
            expiration=expiration,
            strict=strict,
            refresh=refresh,
        )
    except MediaPipelineError as exc:
        response.data = {'detail': str(exc), 'code': exc.code}
        response.status_code = exc.status_code
    return response


class ArtistPanelSignedR2Middleware:
    """Ensure every R2 URL returned by Artist Panel APIs is signed."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_template_response(self, request, response):
        prefix = getattr(settings, 'ARTIST_API_PREFIX', '/api/artist/')
        if not request.path.startswith(prefix):
            return response
        return _sign_response(
            response,
            expiration=getattr(settings, 'ARTIST_R2_SIGNED_URL_TTL', 3600),
        )


class AdminPanelSignedR2Middleware:
    """Sign private media once at the admin API response boundary."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_template_response(self, request, response):
        prefix = getattr(settings, 'ADMIN_API_PREFIX', '/api/admin/')
        if not request.path.startswith(prefix):
            return response
        return _sign_response(
            response,
            expiration=getattr(settings, 'ADMIN_R2_SIGNED_URL_TTL', 3600),
            strict=False,
        )

class ClientHomeCacheControlMiddleware:
    """Prevent browser/proxy caching of Home responses with expiring signed media."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        prefix = getattr(settings, 'HOME_API_PREFIX', '/api/home/')
        if request.path.startswith(prefix):
            response['Cache-Control'] = 'private, no-store'
        return response

