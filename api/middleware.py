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


def _sign_response(response, *, expiration, refresh=False):
    if not hasattr(response, 'data'):
        return response
    try:
        response.data = sign_r2_urls_in_payload(
            response.data,
            expiration=expiration,
            strict=True,
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


class ClientHomeSignedR2Middleware:
    """Re-sign every R2 media URL returned by client-home APIs on each request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_template_response(self, request, response):
        prefix = getattr(settings, 'HOME_API_PREFIX', '/api/home/')
        if not request.path.startswith(prefix):
            return response

        response = _sign_response(
            response,
            expiration=getattr(settings, 'HOME_R2_SIGNED_URL_TTL', 3600),
            refresh=True,
        )
        # Prevent a browser or reverse proxy from serving a serialized home
        # response after the embedded R2 signatures have expired. Redis still
        # caches rankings/payloads server-side, and this middleware re-signs
        # those cached payloads immediately before every response.
        response['Cache-Control'] = 'private, no-store, max-age=0'
        response['Pragma'] = 'no-cache'
        return response
