"""Stable error responses for authentication and session endpoints.

The client branches on ``error.code`` and never needs to parse human text.
Messages remain English at source and are localized by LocalizedJSONRenderer.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    MethodNotAllowed,
    NotAuthenticated,
    NotFound,
    ParseError,
    PermissionDenied,
    Throttled,
    UnsupportedMediaType,
    ValidationError,
)
import logging

logger = logging.getLogger(__name__)

AUTH_ERROR_MESSAGES: dict[str, str] = {
    "VALIDATION_ERROR": "Please correct the highlighted fields.",
    "INVALID_PHONE": "Enter a valid mobile number.",
    "USER_EXISTS": "Phone already registered",
    "USER_BANNED": "This account has been banned.",
    "RATE_LIMIT": "Please wait before requesting another OTP",
    "SMS_FAILED": "Failed to send OTP SMS",
    "OTP_NOT_FOUND": "No valid OTP found",
    "OTP_EXCEEDED": "OTP attempts exceeded",
    "OTP_INVALID": "The provided OTP is invalid.",
    "AUTH_FAILED": "Invalid credentials",
    "ACCOUNT_LOCKED": "Account temporarily locked",
    "PHONE_NOT_REGISTERED": "Phone not registered",
    "TOKEN_INVALID": "Invalid refresh token",
    "TOKEN_REVOKED": "Session has been revoked or expired",
    "REFRESH_TOKEN_REQUIRED": "refreshToken is required to keep the current session",
    "INVALID_PASSWORD": "Current password is incorrect",
    "SESSION_NOT_FOUND": "Session not found",
    "CURRENT_SESSION_INVALID": "The current session is invalid or has expired.",
    "ARTIST_ONLY": "Only artists can access this endpoint",
    "SUBMISSION_EXISTS": "Submission already exists. Use PATCH to update.",
    "ARTIST_AUTH_NOT_FOUND": "Artist authentication submission not found",
    "ARTIST_ACCOUNT_NOT_FOUND": "No artist account is registered for this phone number.",
    "ARTIST_RESET_TOKEN_INVALID": "This password reset session is invalid. Request a new code.",
    "ARTIST_RESET_TOKEN_EXPIRED": "This password reset session has expired. Request a new code.",
    "ARTIST_RESET_TOKEN_USED": "This password reset session has already been used. Request a new code.",
    "BAD_REQUEST": "The request is invalid.",
    "AUTHENTICATION_REQUIRED": "Authentication is required.",
    "PERMISSION_DENIED": "You do not have permission to perform this action.",
    "NOT_FOUND": "The requested resource was not found.",
    "SERVER_ERROR": "The server could not complete the request.",
    "INVALID_JSON": "The request body is not valid JSON.",
    "METHOD_NOT_ALLOWED": "This request method is not allowed.",
    "UNSUPPORTED_MEDIA_TYPE": "The request content type is not supported.",
}


def _error_item(value: Any) -> dict[str, str]:
    return {
        "code": str(getattr(value, "code", "invalid") or "invalid"),
        "message": str(value),
    }


def _normalize_field_errors(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize_field_errors(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_field_errors(item) for item in value]
    return _error_item(value)


def auth_error(
    code: str,
    status_code: int,
    *,
    message: str | None = None,
    fields: Any | None = None,
    retry_after_seconds: int | None = None,
    meta: Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> Response:
    payload: dict[str, Any] = {
        "code": code,
        "message": message or AUTH_ERROR_MESSAGES.get(code, "The request could not be completed."),
    }
    if fields:
        payload["fields"] = _normalize_field_errors(fields)
    if retry_after_seconds is not None:
        payload["retry_after_seconds"] = max(1, int(retry_after_seconds))
    response_meta = dict(meta or {})
    if request_id:
        response_meta["request_id"] = request_id
    if response_meta:
        payload["meta"] = response_meta
    response = Response({"error": payload}, status=status_code)
    if retry_after_seconds is not None:
        response["Retry-After"] = str(max(1, int(retry_after_seconds)))
    if request_id:
        response["X-Request-ID"] = request_id
    return response


def validation_error(errors: Any) -> Response:
    return auth_error(
        "VALIDATION_ERROR",
        status.HTTP_400_BAD_REQUEST,
        fields=errors,
    )


class AuthAPIView(APIView):
    """APIView with a stable JSON error contract for authentication routes."""

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        # Authentication/session payloads must never be stored by browsers,
        # shared proxies or CDNs. Vary keeps bilingual responses cache-safe.
        response["Cache-Control"] = "no-store, private"
        response["Pragma"] = "no-cache"
        existing_vary = response.get("Vary", "")
        vary_values = {item.strip() for item in existing_vary.split(",") if item.strip()}
        vary_values.add("Accept-Language")
        response["Vary"] = ", ".join(sorted(vary_values))
        return response

    def handle_exception(self, exc):
        if isinstance(exc, ValidationError):
            return validation_error(exc.detail)
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return auth_error("AUTHENTICATION_REQUIRED", status.HTTP_401_UNAUTHORIZED)
        if isinstance(exc, PermissionDenied):
            message = str(getattr(exc, "detail", ""))
            code = "USER_BANNED" if "banned" in message.lower() else "PERMISSION_DENIED"
            return auth_error(code, status.HTTP_403_FORBIDDEN)
        if isinstance(exc, NotFound):
            return auth_error("NOT_FOUND", status.HTTP_404_NOT_FOUND)
        if isinstance(exc, Throttled):
            return auth_error(
                "RATE_LIMIT",
                status.HTTP_429_TOO_MANY_REQUESTS,
                retry_after_seconds=max(1, int(exc.wait or 1)),
            )
        if isinstance(exc, ParseError):
            return auth_error("INVALID_JSON", status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, MethodNotAllowed):
            return auth_error(
                "METHOD_NOT_ALLOWED",
                status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        if isinstance(exc, UnsupportedMediaType):
            return auth_error("UNSUPPORTED_MEDIA_TYPE", status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
        if isinstance(exc, APIException):
            # Preserve the HTTP status while keeping a stable, non-sensitive code.
            status_code = int(getattr(exc, "status_code", status.HTTP_400_BAD_REQUEST))
            code = "SERVER_ERROR" if status_code >= 500 else "BAD_REQUEST"
            return auth_error(code, status_code)

        request_id = uuid4().hex[:16]
        request = getattr(self, "request", None)
        logger.exception(
            "Unhandled authentication endpoint error request_id=%s method=%s path=%s",
            request_id,
            getattr(request, "method", "unknown"),
            getattr(request, "path", "unknown"),
        )
        return auth_error(
            "SERVER_ERROR",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            request_id=request_id,
        )
