from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from django.utils import timezone

from .models import RefreshToken
from .subscriptions import normalize_expired_premium


class OptionalJWTAuthentication(JWTAuthentication):
    """Use a valid JWT when present; let public endpoints continue as guest otherwise."""

    def authenticate(self, request):
        try:
            authenticated = super().authenticate(request)
            if authenticated is not None:
                user, token = authenticated
                session_id = token.get("session_id")
                if session_id is None:
                    # Access tokens issued before per-device binding cannot be
                    # mapped safely to a session. Force one refresh so active
                    # devices receive a bound token; revoked devices cannot.
                    raise AuthenticationFailed("Token session is revoked.", code="token_revoked")
                session_is_active = RefreshToken.objects.filter(
                    pk=session_id,
                    user_id=user.id,
                    revoked_at__isnull=True,
                    expires_at__gt=timezone.now(),
                ).exists()
                if not session_is_active:
                    raise AuthenticationFailed("Token session is revoked.", code="token_revoked")
                normalize_expired_premium(user)
                return user, token
            return None
        except AuthenticationFailed as error:
            if getattr(error, "code", "") == "token_revoked":
                raise
            return None
        except (InvalidToken, TokenError, UnicodeError, TypeError, ValueError):
            return None
