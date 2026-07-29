import mimetypes
import os
from pathlib import Path

from django.conf import settings
from django.core.exceptions import SuspiciousFileOperation
from django.http import FileResponse, Http404
from django.utils._os import safe_join
from django.views import View


class PublicMediaFileView(View):
    """Small ASGI-safe fallback for public uploaded media.

    Production Nginx may serve ``/media/`` directly when the media volume is
    mounted into that container.  When it is not, the existing catch-all proxy
    reaches this view instead of returning Django's production 404.  This keeps
    user avatars available without enabling Django's DEBUG-only static helper.
    """

    http_method_names = ["get", "head", "options"]

    def get(self, request, path: str, *args, **kwargs):
        normalized_path = str(path or '').replace('\\', '/').lstrip('/')
        if not normalized_path.startswith('user_profiles/'):
            raise Http404

        try:
            full_path = Path(safe_join(settings.MEDIA_ROOT, normalized_path))
        except SuspiciousFileOperation as exc:
            raise Http404 from exc

        if not full_path.is_file():
            raise Http404

        content_type, content_encoding = mimetypes.guess_type(str(full_path))
        response = FileResponse(
            full_path.open("rb"),
            content_type=content_type or "application/octet-stream",
        )
        response["Content-Length"] = str(os.path.getsize(full_path))
        response["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=604800"
        response["X-Content-Type-Options"] = "nosniff"
        if content_encoding:
            response["Content-Encoding"] = content_encoding
        return response

    def head(self, request, path: str, *args, **kwargs):
        response = self.get(request, path, *args, **kwargs)
        response.streaming_content = iter(())
        return response
