"""JWT authentication middleware for browser WebSocket connections."""
from __future__ import annotations

import logging
from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from .models import RefreshToken

logger = logging.getLogger(__name__)
JWT_SUBPROTOCOL_PREFIX = "jwt."
MAX_TOKEN_LENGTH = 4096


def _header_value(scope, name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1", errors="ignore").strip()
    return ""


def _protocol_candidates(scope) -> list[str]:
    candidates = [
        str(protocol).strip()
        for protocol in scope.get("subprotocols", ())
        if str(protocol).strip()
    ]

    # Daphne normally populates ``scope['subprotocols']``. Keep a raw-header
    # fallback for proxies/ASGI servers that forward the header but do not parse
    # it into the scope list.
    raw_header = _header_value(scope, b"sec-websocket-protocol")
    if raw_header:
        for protocol in raw_header.split(","):
            value = protocol.strip()
            if value and value not in candidates:
                candidates.append(value)
    return candidates


def _extract_token(scope) -> tuple[str | None, str]:
    # Preferred browser transport: Sec-WebSocket-Protocol. Unlike a query
    # parameter, this keeps the access token out of normal URL/access logs.
    for protocol in _protocol_candidates(scope):
        if protocol.startswith(JWT_SUBPROTOCOL_PREFIX):
            token = protocol[len(JWT_SUBPROTOCOL_PREFIX):].strip()
            if token and len(token) <= MAX_TOKEN_LENGTH:
                return token, "subprotocol"

    # Compatibility for non-browser clients that can send Authorization.
    authorization = _header_value(scope, b"authorization")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token and len(token) <= MAX_TOKEN_LENGTH:
            return token, "authorization"

    # Final compatibility fallback for constrained clients/proxies. The web app
    # does not use this path, keeping its JWT out of URLs and Nginx access logs.
    query = parse_qs(scope.get("query_string", b"").decode("utf-8", errors="ignore"))
    candidates = query.get("token") or query.get("access_token") or []
    if candidates:
        token = candidates[0].strip()
        if token and len(token) <= MAX_TOKEN_LENGTH:
            return token, "query"

    return None, "missing"


@database_sync_to_async
def _authenticate_token(raw_token: str):
    authentication = JWTAuthentication()
    try:
        validated_token = authentication.get_validated_token(raw_token)
        user = authentication.get_user(validated_token)
    except AuthenticationFailed:
        return AnonymousUser(), "authentication_failed"
    except InvalidToken:
        return AnonymousUser(), "invalid_token"
    except TokenError:
        return AnonymousUser(), "token_error"
    except (KeyError, TypeError, ValueError):
        return AnonymousUser(), "malformed_token"
    except Exception:
        logger.exception("Unexpected WebSocket JWT authentication failure")
        return AnonymousUser(), "authentication_error"

    if not user:
        return AnonymousUser(), "user_missing"
    if not user.is_active:
        return AnonymousUser(), "user_inactive"
    session_id = validated_token.get("session_id")
    if session_id is not None and not RefreshToken.objects.filter(
        pk=session_id,
        user_id=user.id,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).exists():
        return AnonymousUser(), "session_revoked"
    return user, "ok"


class JWTAuthMiddleware:
    """Resolve ``scope['user']`` from a SimpleJWT access token."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        scoped = dict(scope)
        raw_token, token_source = _extract_token(scoped)
        if raw_token:
            user, auth_status = await _authenticate_token(raw_token)
        else:
            user, auth_status = AnonymousUser(), "token_missing"

        scoped["user"] = user
        scoped["ws_auth_status"] = auth_status
        scoped["ws_token_source"] = token_source
        return await self.inner(scoped, receive, send)
