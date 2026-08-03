from django.conf import settings

from .utils import MediaPipelineError, sign_r2_urls_in_payload


class ArtistPanelSignedR2Middleware:
    """Ensure every R2 URL returned by Artist Panel APIs is short-lived and signed."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_template_response(self, request, response):
        prefix = getattr(settings, 'ARTIST_API_PREFIX', '/api/artist/')
        if not request.path.startswith(prefix) or not hasattr(response, 'data'):
            return response

        try:
            response.data = sign_r2_urls_in_payload(
                response.data,
                expiration=getattr(settings, 'ARTIST_R2_SIGNED_URL_TTL', 3600),
                strict=True,
            )
        except MediaPipelineError as exc:
            response.data = {'detail': str(exc), 'code': exc.code}
            response.status_code = exc.status_code
        return response
