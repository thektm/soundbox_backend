from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from .subscriptions import normalize_expired_premium


class OptionalJWTAuthentication(JWTAuthentication):
    """Use a valid JWT when present; let public endpoints continue as guest otherwise."""

    def authenticate(self, request):
        try:
            authenticated = super().authenticate(request)
            if authenticated is not None:
                user, token = authenticated
                normalize_expired_premium(user)
                return user, token
            return None
        except (AuthenticationFailed, InvalidToken, TokenError, UnicodeError, TypeError, ValueError):
            return None
