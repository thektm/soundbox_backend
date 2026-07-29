"""JWT authentication middleware for browser WebSocket connections."""
from __future__ import annotations

from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

JWT_SUBPROTOCOL_PREFIX = "jwt."


def _extract_token(scope) -> str | None:
    # Preferred browser transport: Sec-WebSocket-Protocol. Unlike a query
    # parameter, this keeps the access token out of normal URL/access logs.
    for protocol in scope.get("subprotocols", ()):
        if protocol.startswith(JWT_SUBPROTOCOL_PREFIX):
            token = protocol[len(JWT_SUBPROTOCOL_PREFIX):].strip()
            if token and len(token) <= 4096:
                return token

    # Compatibility fallback for non-browser clients and constrained proxies.
    query = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="ignore"))
    candidates = query.get("token") or query.get("access_token") or []
    if not candidates:
        return None
    token = candidates[0].strip()
    return token if token and len(token) <= 4096 else None


@database_sync_to_async
def _authenticate_token(raw_token: str):
    authentication = JWTAuthentication()
    try:
        validated_token = authentication.get_validated_token(raw_token)
        user = authentication.get_user(validated_token)
    except (AuthenticationFailed, InvalidToken, TokenError, KeyError, TypeError, ValueError):
        return AnonymousUser()

    if not user or not user.is_active:
        return AnonymousUser()
    return user


class JWTAuthMiddleware:
    """Resolve ``scope['user']`` from a SimpleJWT access token."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        scoped = dict(scope)
        raw_token = _extract_token(scoped)
        scoped["user"] = (
            await _authenticate_token(raw_token) if raw_token else AnonymousUser()
        )
        return await self.inner(scoped, receive, send)
