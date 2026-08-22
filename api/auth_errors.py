"""Stable error responses for authentication and session endpoints.

The client branches on ``error.code`` and never needs to parse human text.
Authentication endpoints expose stable machine-readable codes with precise Persian messages.
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
    "VALIDATION_ERROR": "لطفاً اطلاعات مشخص‌شده را بررسی و اصلاح کنید.",
    "INVALID_PHONE": "شماره تلفن همراه معتبر وارد کنید.",
    "USER_EXISTS": "این شماره تلفن قبلاً ثبت شده است. برای ادامه وارد حساب خود شوید.",
    "USER_BANNED": "حساب شما مسدود شده است. برای پیگیری با پشتیبانی تماس بگیرید.",
    "RATE_LIMIT": "تعداد درخواست‌ها بیش از حد مجاز است. کمی صبر کنید و دوباره تلاش کنید.",
    "SMS_FAILED": "ارسال پیامک کد تأیید انجام نشد. چند دقیقه دیگر دوباره تلاش کنید.",
    "OTP_NOT_FOUND": "کد تأیید فعال پیدا نشد. یک کد جدید درخواست کنید.",
    "OTP_EXCEEDED": "تعداد تلاش‌های ناموفق بیش از حد مجاز است. کد تأیید جدیدی درخواست کنید.",
    "OTP_INVALID": "کد تأیید واردشده صحیح نیست. دوباره بررسی کنید.",
    "AUTH_FAILED": "شماره تلفن یا رمز عبور اشتباه است. لطفاً دوباره تلاش کنید.",
    "ACCOUNT_LOCKED": "به‌دلیل چند تلاش ناموفق، ورود موقتاً قفل شده است. کمی بعد دوباره تلاش کنید.",
    "PHONE_NOT_REGISTERED": "حسابی با این شماره تلفن پیدا نشد.",
    "TOKEN_INVALID": "نشست ورود معتبر نیست. دوباره وارد شوید.",
    "TOKEN_REVOKED": "نشست شما منقضی یا لغو شده است. دوباره وارد شوید.",
    "REFRESH_TOKEN_REQUIRED": "اطلاعات تمدید نشست ارسال نشده است. دوباره وارد شوید.",
    "INVALID_PASSWORD": "رمز عبور فعلی صحیح نیست.",
    "SESSION_NOT_FOUND": "نشست موردنظر پیدا نشد.",
    "CURRENT_SESSION_REVOKE_FORBIDDEN": "نشست فعلی شما قابل لغو نیست.",
    "CURRENT_SESSION_INVALID": "نشست فعلی معتبر نیست یا منقضی شده است. دوباره وارد شوید.",
    "ARTIST_ONLY": "این بخش فقط برای حساب هنرمند در دسترس است.",
    "SUBMISSION_EXISTS": "درخواست احراز هویت هنرمند قبلاً ثبت شده است و باید همان درخواست را ویرایش کنید.",
    "ARTIST_AUTH_NOT_FOUND": "درخواست احراز هویت هنرمند پیدا نشد.",
    "ARTIST_ACCOUNT_NOT_FOUND": "برای این شماره تلفن حساب هنرمند فعالی پیدا نشد.",
    "ARTIST_RESET_TOKEN_INVALID": "نشست بازیابی رمز عبور معتبر نیست. دوباره کد بازیابی درخواست کنید.",
    "ARTIST_RESET_TOKEN_EXPIRED": "لینک بازنشانی رمز عبور منقضی شده است. دوباره درخواست بازنشانی بدهید.",
    "ARTIST_RESET_TOKEN_USED": "این لینک بازنشانی قبلاً استفاده شده است. دوباره درخواست بازنشانی بدهید.",
    "BAD_REQUEST": "اطلاعات درخواست معتبر نیست.",
    "AUTHENTICATION_REQUIRED": "برای ادامه باید دوباره وارد حساب هنرمند شوید.",
    "PERMISSION_DENIED": "اجازه انجام این عملیات را ندارید.",
    "NOT_FOUND": "اطلاعات درخواستی پیدا نشد.",
    "SERVER_ERROR": "سرور نتوانست درخواست را کامل کند. کمی بعد دوباره تلاش کنید.",
    "INVALID_JSON": "ساختار اطلاعات ارسالی معتبر نیست.",
    "METHOD_NOT_ALLOWED": "این روش برای درخواست فعلی مجاز نیست.",
    "UNSUPPORTED_MEDIA_TYPE": "نوع محتوای ارسالی پشتیبانی نمی‌شود.",
}



_VALIDATION_MESSAGES_FA = {
    "required": "این فیلد الزامی است.",
    "blank": "این فیلد نمی‌تواند خالی باشد.",
    "null": "این فیلد نمی‌تواند خالی باشد.",
    "invalid": "مقدار واردشده معتبر نیست.",
    "invalid_choice": "گزینه انتخاب‌شده معتبر نیست.",
    "unique": "این مقدار قبلاً ثبت شده است.",
    "does_not_exist": "اطلاعات مرتبط پیدا نشد.",
    "incorrect_type": "نوع مقدار واردشده معتبر نیست.",
    "min_value": "مقدار واردشده کمتر از حد مجاز است.",
    "max_value": "مقدار واردشده بیشتر از حد مجاز است.",
    "min_length": "مقدار واردشده کوتاه‌تر از حد مجاز است.",
    "max_length": "مقدار واردشده طولانی‌تر از حد مجاز است.",
    "invalid_phone": "شماره تلفن همراه معتبر وارد کنید.",
    "invalid_otp_format": "کد تأیید چهاررقمی را کامل وارد کنید.",
    "invalid_password": "رمز عبور واردشده صحیح نیست.",
    "password_unchanged": "رمز عبور جدید باید با رمز عبور فعلی متفاوت باشد.",
    "invalid_image": "فایل تصویر معتبر نیست.",
    "invalid_file": "فایل ارسال‌شده معتبر نیست.",
    "empty": "فایل ارسال‌شده خالی است.",
}


def _contains_persian(value: str) -> bool:
    return any("\u0600" <= char <= "\u06ff" for char in value)


def _error_item(value: Any) -> dict[str, str]:
    code = str(getattr(value, "code", "invalid") or "invalid")
    source = str(value or "").strip()

    # A deliberately written Persian serializer/endpoint message is more
    # precise than DRF's broad error code (often just ``invalid``), so keep it.
    if source and _contains_persian(source):
        return {"code": code, "message": source}

    mapped = _VALIDATION_MESSAGES_FA.get(code)
    if mapped:
        return {"code": code, "message": mapped}

    # Never leak an unexpected English DRF/server validation string.
    return {"code": code, "message": "مقدار واردشده معتبر نیست."}


def _normalize_field_errors(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize_field_errors(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_field_errors(item) for item in value]
    return _error_item(value)


def _fa_number(value: int) -> str:
    return str(int(value)).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


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
    resolved_message = message or AUTH_ERROR_MESSAGES.get(code, "انجام درخواست ممکن نشد. لطفاً دوباره تلاش کنید.")
    if retry_after_seconds is not None:
        wait = max(1, int(retry_after_seconds))
        if code == "ACCOUNT_LOCKED":
            resolved_message = f"به‌دلیل چند تلاش ناموفق، ورود موقتاً قفل شده است. {_fa_number(wait)} ثانیه دیگر دوباره تلاش کنید."
        elif code == "RATE_LIMIT":
            resolved_message = f"تعداد درخواست‌ها بیش از حد مجاز است. {_fa_number(wait)} ثانیه دیگر دوباره تلاش کنید."
    payload: dict[str, Any] = {
        "code": code,
        "message": resolved_message,
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
