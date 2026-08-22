from django.utils import timezone
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core import signing
from django.core.signing import BadSignature, SignatureExpired
import logging
import requests
import hmac
from requests.adapters import HTTPAdapter
from django.contrib.auth.hashers import make_password, check_password
from django.db import transaction
from django.db.models import F
import user_agents
from rest_framework.response import Response
from rest_framework import status, serializers
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.exceptions import PermissionDenied
from .models import User, OtpCode, RefreshToken
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, inline_serializer
from .serializers import (
    RegisterRequestSerializer,
    VerifySerializer,
    LoginPasswordSerializer,
    LoginOtpRequestSerializer,
    LoginOtpVerifySerializer,
    ForgotPasswordSerializer,
    PasswordResetSerializer,
    ArtistPasswordResetSerializer,
    TokenRefreshRequestSerializer,
    LogoutSerializer,
    ChangePasswordSerializer,
    ArtistAuthSerializer,
    SessionSerializer,
)
from rest_framework_simplejwt.tokens import RefreshToken as SimpleRefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from django.utils.crypto import get_random_string
from datetime import timedelta
import hashlib
import re
from .recommendation_runtime import redis_delete, redis_get, redis_set
from .auth_errors import AuthAPIView, auth_error, validation_error
from .utils import MediaPipelineError
from .admin_permissions import employee_session_version, is_employee

_SMS_SESSION = requests.Session()
_SMS_ADAPTER = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
_SMS_SESSION.mount('https://', _SMS_ADAPTER)


def normalize_phone(phone: str) -> str:
    digits = ''.join(ch for ch in (phone or '') if ch.isdigit())
    # If number starts with country code '98' and then 9 digits, transform to local '09...'
    if digits.startswith('98') and len(digits) == 11:
        return '0' + digits[2:]
    if digits.startswith('0098') and len(digits) == 13:
        return '0' + digits[4:]
    if digits.startswith('+98'):
        # unlikely as + removed, but handle
        if digits.startswith('98'):
            return '0' + digits[2:]
    # If already local 09xxxxxxxxx (11 digits)
    if len(digits) == 11 and digits.startswith('09'):
        return digits
    return digits


def generate_otp(length=4):
    # generate digits only
    return get_random_string(length=length, allowed_chars='0123456789')


def generate_unique_numeric_id(length=10):
    """Generate a unique numeric-only string for `unique_id` field."""
    while True:
        new_id = get_random_string(length=length, allowed_chars='0123456789')
        if not User.objects.filter(unique_id=new_id).exists():
            return new_id


def parse_artist_flag(request) -> bool:
    """Read `artist` flag from query params only (no body fallback)."""
    # Primary source: query params (preferred for public API calls)
    try:
        val = request.query_params.get('artist')
    except Exception:
        val = None
    # Fallback: allow `artist` in JSON body for clients that send it there
    if val is None:
        try:
            val = request.data.get('artist') if hasattr(request, 'data') else None
        except Exception:
            val = None
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).lower() in ('1', 'true', 'yes', 'on')



_ARTIST_RESET_TOKEN_SALT = 'sedabox.artist-password-reset.v1'


def _artist_reset_ttl_seconds() -> int:
    return max(60, int(getattr(settings, 'ARTIST_PASSWORD_RESET_TOKEN_TTL_SECONDS', 600)))


def _artist_password_fingerprint(user: User) -> str:
    return hashlib.sha256((user.artist_password or '').encode('utf-8')).hexdigest()


def _artist_reset_token(user: User) -> str:
    return signing.dumps(
        {
            'sub': user.pk,
            'phone': user.phone_number,
            'purpose': OtpCode.PURPOSE_ARTIST_RESET,
            'password_fingerprint': _artist_password_fingerprint(user),
        },
        key=settings.SECRET_KEY,
        salt=_ARTIST_RESET_TOKEN_SALT,
        compress=True,
    )


def _load_artist_reset_token(token: str) -> dict:
    return signing.loads(
        token,
        key=settings.SECRET_KEY,
        salt=_ARTIST_RESET_TOKEN_SALT,
        max_age=_artist_reset_ttl_seconds(),
    )


def _artist_account_for_password_reset(phone: str):
    user = User.objects.filter(phone_number=normalize_phone(phone)).first()
    if user is None or (
        User.ROLE_ARTIST not in (user.roles or []) and not user.artist_password
    ):
        return None, auth_error('ARTIST_ACCOUNT_NOT_FOUND', status.HTTP_404_NOT_FOUND)
    if user.is_banned:
        return None, auth_error('USER_BANNED', status.HTTP_403_FORBIDDEN)
    return user, None

def _fast_secret_hash(namespace: str, value: str) -> str:
    secret = str(settings.SECRET_KEY).encode('utf-8')
    digest = hmac.new(secret, f'{namespace}:{value}'.encode('utf-8'), hashlib.sha256).hexdigest()
    return f'{namespace}${digest}'


def hash_code(code: str) -> str:
    # OTPs are short-lived and rate-limited. A keyed HMAC is secure here and is
    # dramatically faster than a password KDF designed for long-lived passwords.
    return _fast_secret_hash('otp-v2', code)


def check_code_hash(raw: str, hashed: str) -> bool:
    if hashed and hashed.startswith('otp-v2$'):
        return hmac.compare_digest(hash_code(raw), hashed)
    # Backward compatibility for OTP rows created before this optimization.
    return check_password(raw, hashed)


def hash_refresh_token(token: str) -> str:
    return _fast_secret_hash('refresh-v2', token)


def check_refresh_token(token: str, hashed: str) -> bool:
    if hashed and hashed.startswith('refresh-v2$'):
        return hmac.compare_digest(hash_refresh_token(token), hashed)
    return check_password(token, hashed)


def _otp_latest_key(user_id: int, purpose: str) -> str:
    return f'sedabox:otp:latest:{int(user_id)}:{purpose}'


def _otp_send_guard_key(phone: str, purpose: str) -> str:
    digest = hashlib.sha256(f'{normalize_phone(phone)}:{purpose}'.encode()).hexdigest()[:24]
    return f'sedabox:otp:send-guard:{digest}'


def otp_retry_after(phone: str, purpose: str) -> int:
    """Acquire a Redis cooldown guard; return zero when sending is allowed."""
    cooldown = max(1, int(getattr(settings, 'OTP_SEND_COOLDOWN_SECONDS', 60)))
    key = _otp_send_guard_key(phone, purpose)
    if redis_set(key, timezone.now().timestamp(), cooldown, only_if_absent=True):
        return 0
    client_value = redis_get(key)
    if client_value is not None:
        try:
            elapsed = max(0, int(timezone.now().timestamp() - float(client_value)))
            return max(1, cooldown - elapsed)
        except (TypeError, ValueError):
            return cooldown
    # Redis unavailable: caller may fall back to the indexed database lookup.
    return -1


def otp_rate_limit_retry_after(phone: str, purpose: str, user: User | None = None) -> int:
    """Return seconds to wait, while atomically reserving an allowed send slot."""
    retry_after = otp_retry_after(phone, purpose)
    if retry_after >= 0:
        return retry_after
    if user is None:
        return 0
    cooldown = max(1, int(getattr(settings, 'OTP_SEND_COOLDOWN_SECONDS', 60)))
    last_created = OtpCode.objects.filter(
        user=user, purpose=purpose
    ).order_by('-created_at').values_list('created_at', flat=True).first()
    if last_created is None:
        return 0
    remaining = cooldown - int((timezone.now() - last_created).total_seconds())
    return max(0, remaining)


def release_otp_send_guard(phone: str, purpose: str) -> None:
    redis_delete(_otp_send_guard_key(phone, purpose))


def latest_valid_otp(user: User, purpose: str, *, for_update: bool = False):
    queryset = OtpCode.objects
    if for_update:
        queryset = queryset.select_for_update()
    cached_id = redis_get(_otp_latest_key(user.pk, purpose))
    if cached_id:
        otp = queryset.filter(
            pk=cached_id, user=user, purpose=purpose, consumed=False,
            expires_at__gt=timezone.now(),
        ).first()
        if otp is not None:
            return otp
    otp = queryset.filter(
        user=user, purpose=purpose, consumed=False, expires_at__gt=timezone.now(),
    ).order_by('-created_at').first()
    if otp is not None:
        ttl = max(60, int((otp.expires_at - timezone.now()).total_seconds()) + 60)
        redis_set(_otp_latest_key(user.pk, purpose), otp.pk, ttl)
    else:
        redis_delete(_otp_latest_key(user.pk, purpose))
    return otp


def consume_otp(user: User, purpose: str, raw_code: str, *, max_attempts: int = 3) -> str:
    """Atomically validate and consume the newest OTP with one indexed lookup.

    Returns one of: ``ok``, ``not_found``, ``exceeded`` or ``invalid``.
    Row locking prevents two simultaneous verify requests from consuming the same
    code and removes the previous exists()+first() double-query pattern.
    """
    cache_key = _otp_latest_key(user.pk, purpose)
    with transaction.atomic():
        otp_obj = latest_valid_otp(user, purpose, for_update=True)
        if otp_obj is None:
            return 'not_found'
        if otp_obj.attempts >= max_attempts:
            OtpCode.objects.filter(pk=otp_obj.pk).update(consumed=True)
            redis_delete(cache_key)
            return 'exceeded'
        if not check_code_hash(raw_code or '', otp_obj.code_hash):
            OtpCode.objects.filter(pk=otp_obj.pk).update(attempts=F('attempts') + 1)
            return 'invalid'
        OtpCode.objects.filter(pk=otp_obj.pk).update(consumed=True)
        if purpose in {OtpCode.PURPOSE_VERIFY, OtpCode.PURPOSE_LOGIN} and not user.is_verified:
            User.objects.filter(pk=user.pk).update(is_verified=True)
            user.is_verified = True
        redis_delete(cache_key)
        return 'ok'


def _otp_failure_response(result: str):
    mapping = {
        'not_found': 'OTP_NOT_FOUND',
        'exceeded': 'OTP_EXCEEDED',
        'invalid': 'OTP_INVALID',
    }
    code = mapping.get(result)
    return auth_error(code, status.HTTP_401_UNAUTHORIZED) if code else None


def get_device_info(request):
    """Extract device info from User-Agent and request data"""
    ua_string = request.META.get('HTTP_USER_AGENT', '')
    user_agent = user_agents.parse(ua_string)
    
    # Default from User-Agent
    device_name = user_agent.device.family
    if user_agent.device.brand:
        device_name = f"{user_agent.device.brand} {user_agent.device.model}"
    
    device_type = "PC"
    if user_agent.is_mobile:
        device_type = "Mobile"
    elif user_agent.is_tablet:
        device_type = "Tablet"
    elif user_agent.is_bot:
        device_type = "Bot"
        
    os_info = f"{user_agent.os.family} {user_agent.os.version_string}"
    
    # Override with client-provided data if available
    device_name = request.data.get('device_name') or device_name
    device_type = request.data.get('device_type') or device_type
    os_info = request.data.get('os_info') or os_info
    
    return device_name, device_type, os_info


def send_sms(phone: str, code: str, purpose: str, minutes: int = 5) -> bool:
    # Only Kavenegar is supported (no fallbacks). Fail if not configured.
    logger = logging.getLogger(__name__)
    provider = getattr(settings, 'SMS_PROVIDER', None)
    if provider != 'kavenegar':
        logger.error('SMS_PROVIDER must be set to "kavenegar" in settings')
        return False

    api_key = getattr(settings, 'KAVENEGAR_API_KEY', None)
    if not api_key:
        logger.error('KAVENEGAR_API_KEY is not configured in settings')
        return False

    # Map purpose to template names expected by Kavenegar
    template_map = {
        'login': 'login',
        'register': 'register',
        'forgot-pass': 'forgot-pass',
        OtpCode.PURPOSE_LOGIN: 'login',
        OtpCode.PURPOSE_VERIFY: 'register',
        OtpCode.PURPOSE_RESET: 'forgot-pass',
        OtpCode.PURPOSE_ARTIST_RESET: 'forgot-pass',
    }

    template_name = template_map.get(purpose, 'login')

    # Ensure phone is in local format (09xxxxxxxxx)
    receptor = normalize_phone(phone)
    # Basic validation: receptor must look like a local mobile number
    if not receptor or len(receptor) != 11 or not receptor.startswith('09'):
        logger.error('Invalid phone format for Kavenegar receptor: %s', receptor)
        return False

    url = f"https://api.kavenegar.com/v1/{api_key}/verify/lookup.json"
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    data = {
        'receptor': receptor,
        'token': code,
        'template': template_name,
    }

    try:
        logger.debug('Kavenegar request prepared: url=%s template=%s receptor=%s', url, template_name, receptor)
        resp = _SMS_SESSION.post(
            url, data=data, headers=headers,
            timeout=(
                float(getattr(settings, 'OTP_REQUEST_TIMEOUT_CONNECT', 1.5)),
                float(getattr(settings, 'OTP_REQUEST_TIMEOUT_READ', 3.5)),
            ),
        )
        if resp.status_code != 200:
            logger.error('Kavenegar returned non-200 status: %s %s', resp.status_code, resp.text)
            return False
        payload = resp.json()
        provider_status = (payload.get('return') or {}).get('status') if isinstance(payload, dict) else None
        logger.info(
            'Kavenegar accepted SMS for %s (template=%s provider_status=%s)',
            receptor, template_name, provider_status,
        )
        return True
    except Exception as e:
        logger.exception('Error sending SMS via Kavenegar: %s', e)
        return False


def create_and_send_otp(user: User or None, phone: str, purpose: str, minutes=5) -> OtpCode:
    otp = generate_otp(4)
    expires = timezone.now() + timedelta(minutes=minutes)
    logger = logging.getLogger(__name__)

    # Keep only one live OTP per user/purpose. The short transaction prevents
    # concurrent requests from leaving multiple valid codes behind.
    with transaction.atomic():
        if user is not None:
            OtpCode.objects.filter(
                user=user, purpose=purpose, consumed=False
            ).update(consumed=True)
        otp_obj = OtpCode.objects.create(
            user=user,
            code_hash=hash_code(otp),
            code=otp,
            purpose=purpose,
            expires_at=expires,
        )
    if user is not None:
        redis_set(
            _otp_latest_key(user.pk, purpose), otp_obj.pk,
            max(60, int(minutes * 60) + 60),
        )

    sent = send_sms(phone, otp, purpose, minutes)
    if sent:
        logger.info(
            'OTP created and SMS accepted for phone=%s purpose=%s otp_id=%s',
            phone, purpose, otp_obj.pk,
        )
    else:
        # A failed provider call must not leave a valid but undelivered OTP.
        OtpCode.objects.filter(pk=otp_obj.pk).update(consumed=True)
        if user is not None:
            redis_delete(_otp_latest_key(user.pk, purpose))
        release_otp_send_guard(phone, purpose)
        logger.warning(
            'OTP SMS failed for phone=%s purpose=%s otp_id=%s',
            phone, purpose, otp_obj.pk,
        )
    return otp_obj, sent


def issue_tokens_for_user(user: User, request) -> dict:
    if user.is_banned:
        raise PermissionDenied("Your account has been banned.")
    refresh = SimpleRefreshToken.for_user(user)
    if is_employee(user):
        refresh['admin_session_version'] = employee_session_version(user)
    access = refresh.access_token
    # persist hashed refresh token for revocation / rotation tracking
    token_str = str(refresh)
    token_hash = hash_refresh_token(token_str)
    expires_at = timezone.now() + timedelta(days=30)
    
    # Extract device info
    device_name, device_type, os_info = get_device_info(request)
    ua = request.META.get('HTTP_USER_AGENT', '')
    ip = request.META.get('REMOTE_ADDR', '')

    # Find existing session for this device to avoid duplicates
    existing_sessions = RefreshToken.objects.filter(
        user=user,
        user_agent=ua,
        ip=ip,
        device_name=device_name,
        device_type=device_type,
        os_info=os_info
    )

    # One indexed query replaces the previous exists()+first() double hit.
    session = existing_sessions.order_by('-created_at').first()
    if session is not None:
        session.token_hash = token_hash
        session.expires_at = expires_at
        session.revoked_at = None
        session.save(update_fields=['token_hash', 'expires_at', 'revoked_at'])
        existing_sessions.exclude(id=session.id).delete()
    else:
        RefreshToken.objects.create(
            user=user,
            token_hash=token_hash,
            user_agent=ua,
            ip=ip,
            expires_at=expires_at,
            device_name=device_name,
            device_type=device_type,
            os_info=os_info
        )

    # update last_login
    user.last_login_at = timezone.now()
    user.failed_login_attempts = 0
    user.save(update_fields=['last_login_at', 'failed_login_attempts'])
    return {'accessToken': str(access), 'refreshToken': token_str}


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class AuthRegisterView(AuthAPIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="ثبت‌نام کاربر جدید",
        description="ثبت‌نام با شماره موبایل و رمز عبور. در صورت وجود کاربر تایید نشده، کد تایید مجدداً ارسال می‌شود.",
        request=RegisterRequestSerializer,
        responses={
            200: inline_serializer(
                name='AuthRegisterOtpResponse',
                fields={'status': serializers.CharField()}
            ),
            201: inline_serializer(
                name='AuthRegisterArtistResponse',
                fields={'status': serializers.CharField()}
            )
        }
    )
    def post(self, request):
        serializer = RegisterRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = normalize_phone(serializer.validated_data['phone'])
        password = serializer.validated_data['password']
        artist_flag = parse_artist_flag(request)
        admin_flag = bool(serializer.validated_data.get('admin_login', False))
        # when `artist` param is true treat provided `password` as artist password
        artist_password = password if artist_flag else None
        # If user exists
        existing = User.objects.filter(phone_number=phone).first()
        if existing:
            # Any normal registration/login identity must keep audience capability unless it is
            # an explicit admin-only operation. Do not remove existing roles.
            if not admin_flag and not artist_flag and User.ROLE_AUDIENCE not in (existing.roles or []):
                existing.roles.append(User.ROLE_AUDIENCE)
                existing.save(update_fields=['roles'])
            if existing.is_banned:
                return auth_error('USER_BANNED', status.HTTP_403_FORBIDDEN)
            # If already verified, block registration
            if existing.is_verified:
                # If client requested artist role, send a verification OTP to confirm
                if artist_flag:
                    retry_after = otp_rate_limit_retry_after(phone, OtpCode.PURPOSE_VERIFY, existing)
                    if retry_after:
                        return auth_error('RATE_LIMIT', status.HTTP_429_TOO_MANY_REQUESTS, retry_after_seconds=retry_after)
                    # store artist password now so user can verify and become artist (verify will add role)
                    if artist_password:
                        existing.set_artist_password(artist_password)
                        existing.save(update_fields=['artist_password'])
                    otp_obj, sent = create_and_send_otp(existing, phone, OtpCode.PURPOSE_VERIFY)
                    if sent:
                        return Response({'status': 'ok', 'message': 'کد تأیید ارسال شد.'}, status=status.HTTP_200_OK)
                    return auth_error('SMS_FAILED', status.HTTP_503_SERVICE_UNAVAILABLE)
                return auth_error('USER_EXISTS', status.HTTP_409_CONFLICT)
            # Redis performs the common cooldown check without touching PostgreSQL.
            retry_after = otp_rate_limit_retry_after(phone, OtpCode.PURPOSE_VERIFY, existing)
            if retry_after:
                return auth_error('RATE_LIMIT', status.HTTP_429_TOO_MANY_REQUESTS, retry_after_seconds=retry_after)
            # send new OTP to existing unverified user
            otp_obj, sent = create_and_send_otp(existing, phone, OtpCode.PURPOSE_VERIFY)
            if sent:
                return Response({'status': 'ok', 'message': 'کد تأیید ارسال شد.'}, status=status.HTTP_200_OK)
            return auth_error('SMS_FAILED', status.HTTP_503_SERVICE_UNAVAILABLE)

        retry_after = otp_rate_limit_retry_after(phone, OtpCode.PURPOSE_VERIFY)
        if retry_after:
            return auth_error('RATE_LIMIT', status.HTTP_429_TOO_MANY_REQUESTS, retry_after_seconds=retry_after)

        # create user with is_verified False
        create_kwargs = {}
        if artist_flag:
            create_kwargs['roles'] = [User.ROLE_AUDIENCE, User.ROLE_ARTIST]
        if artist_password:
            create_kwargs['artist_password'] = artist_password
        user = User.objects.create_user(phone_number=phone, password=password, **create_kwargs)
        user.is_verified = False
        user.save(update_fields=['is_verified'])
        # create OTP and attempt to send SMS
        otp_obj, sent = create_and_send_otp(user, phone, OtpCode.PURPOSE_VERIFY)
        if sent:
            return Response({'status': 'ok', 'message': 'کد تأیید ارسال شد.'}, status=status.HTTP_200_OK)
        # SMS failed: return error with details
        return auth_error('SMS_FAILED', status.HTTP_503_SERVICE_UNAVAILABLE)


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class AuthVerifyView(AuthAPIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="تایید شماره موبایل",
        description="تایید حساب کاربری با استفاده از کد ارسال شده به شماره موبایل.",
        request=VerifySerializer,
        responses={200: __import__('api.serializers', fromlist=['']).UserSerializer}
    )
    def post(self, request):
        serializer = VerifySerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = normalize_phone(serializer.validated_data['phone'])
        otp = serializer.validated_data['otp']
        artist_flag = parse_artist_flag(request)
        admin_flag = bool(serializer.validated_data.get('admin_login', False))
        # No artist password is accepted in verify body. If artist flag is set,
        # only add the artist role (password should have been provided during registration).
        artist_password = None
        purpose = OtpCode.PURPOSE_VERIFY
        user = User.objects.filter(phone_number=phone).first()
        if user is None:
            return auth_error('PHONE_NOT_REGISTERED', status.HTTP_404_NOT_FOUND)
        if user.is_banned:
            return auth_error('USER_BANNED', status.HTTP_403_FORBIDDEN)
        otp_result = consume_otp(user, purpose, otp)
        otp_error = _otp_failure_response(otp_result)
        if otp_error is not None:
            return otp_error
        user.is_verified = True
        # If client requested artist role during verify, add artist role and set separate artist password
        if artist_flag:
            if User.ROLE_ARTIST not in user.roles:
                user.roles.append(User.ROLE_ARTIST)
            if artist_password:
                user.set_artist_password(artist_password)
        # Ensure a numeric-only string `unique_id` is assigned if missing
        if not user.unique_id:
            user.unique_id = generate_unique_numeric_id(10)
            save_fields = ['is_verified', 'roles'] if artist_flag else ['is_verified']
            if 'unique_id' not in save_fields:
                save_fields.append('unique_id')
            user.save(update_fields=save_fields)
        else:
            user.save(update_fields=['is_verified', 'roles'] if artist_flag else ['is_verified'])
        tokens = issue_tokens_for_user(user, request)
        from .serializers import UserSerializer
        user_data = UserSerializer(user, context={'request': request}).data
        return Response({'accessToken': tokens['accessToken'], 'refreshToken': tokens['refreshToken'], 'user': user_data})


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class LoginPasswordView(AuthAPIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="ورود با رمز عبور",
        description="ورود به حساب کاربری با استفاده از شماره موبایل و رمز عبور (معمولی یا هنرمند).",
        request=LoginPasswordSerializer,
        responses={200: __import__('api.serializers', fromlist=['']).UserSerializer}
    )
    def post(self, request):
        serializer = LoginPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = normalize_phone(serializer.validated_data['phone'])
        password = serializer.validated_data['password']
        artist_flag = parse_artist_flag(request)
        admin_flag = bool(serializer.validated_data.get('admin_login', False))
        try:
            user = User.objects.get(phone_number=phone)
        except User.DoesNotExist:
            return auth_error('AUTH_FAILED', status.HTTP_401_UNAUTHORIZED)
        if user.is_banned:
            return auth_error('USER_BANNED', status.HTTP_403_FORBIDDEN)
        if not user.is_active:
            return auth_error('AUTH_FAILED', status.HTTP_401_UNAUTHORIZED)
        # Audience authentication is only allowed for users who have the audience role.
        # Admin and artist flows are handled separately above and remain isolated.
        if not admin_flag and not artist_flag:
            if User.ROLE_AUDIENCE not in (user.roles or []):
                return auth_error('AUTH_FAILED', status.HTTP_401_UNAUTHORIZED)
        # lockout check
        if user.locked_until and user.locked_until > timezone.now():
            return auth_error('ACCOUNT_LOCKED', status.HTTP_423_LOCKED, retry_after_seconds=max(1, int((user.locked_until - timezone.now()).total_seconds())))
        # choose which password to validate
        password_ok = False
        if admin_flag:
            # Main admin panel uses its own isolated credential.
            # The permission check below remains responsible for authorizing access.
            password_ok = user.check_admin_password(password)
        elif artist_flag:
            password_ok = user.check_artist_password(password)
        else:
            password_ok = user.check_password(password)

        if not password_ok:
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= 5:
                lock_seconds = 15 * 60
                user.locked_until = timezone.now() + timedelta(seconds=lock_seconds)
                user.save(update_fields=['failed_login_attempts', 'locked_until'])
                return auth_error(
                    'ACCOUNT_LOCKED',
                    status.HTTP_423_LOCKED,
                    retry_after_seconds=lock_seconds,
                )
            user.save(update_fields=['failed_login_attempts', 'locked_until'])
            return auth_error('AUTH_FAILED', status.HTTP_401_UNAUTHORIZED)
        # success
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = timezone.now()
        user.save(update_fields=['failed_login_attempts', 'locked_until', 'last_login_at'])
        tokens = issue_tokens_for_user(user, request)
        from .serializers import UserSerializer
        user_data = UserSerializer(user, context={'request': request}).data
        return Response({'accessToken': tokens['accessToken'], 'refreshToken': tokens['refreshToken'], 'user': user_data})


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class LoginOtpRequestView(AuthAPIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="درخواست کد ورود (OTP)",
        description="ارسال کد تایید یکبار مصرف به شماره موبایل برای ورود بدون رمز عبور.",
        request=LoginOtpRequestSerializer,
        responses={
            200: inline_serializer(
                name='LoginOtpRequestResponse',
                fields={'status': serializers.CharField()}
            )
        }
    )
    def post(self, request):
        serializer = LoginOtpRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = normalize_phone(serializer.validated_data['phone'])
        try:
            user = User.objects.get(phone_number=phone)
            if user.is_banned:
                return auth_error('USER_BANNED', status.HTTP_403_FORBIDDEN)
        except User.DoesNotExist:
            return auth_error('PHONE_NOT_REGISTERED', status.HTTP_404_NOT_FOUND)
        retry_after = otp_rate_limit_retry_after(phone, OtpCode.PURPOSE_LOGIN, user)
        if retry_after:
            return auth_error('RATE_LIMIT', status.HTTP_429_TOO_MANY_REQUESTS, retry_after_seconds=retry_after)
        otp_obj, sent = create_and_send_otp(user, phone, OtpCode.PURPOSE_LOGIN)
        if sent:
            return Response({'status': 'otp_sent'}, status=status.HTTP_200_OK)
        return auth_error('SMS_FAILED', status.HTTP_503_SERVICE_UNAVAILABLE)


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class LoginOtpVerifyView(AuthAPIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="ورود با کد تایید (OTP)",
        description="تایید کد یکبار مصرف و دریافت توکن‌های دسترسی.",
        request=LoginOtpVerifySerializer,
        responses={200: __import__('api.serializers', fromlist=['']).UserSerializer}
    )
    def post(self, request):
        serializer = LoginOtpVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = normalize_phone(serializer.validated_data['phone'])
        otp = serializer.validated_data['otp']
        try:
            user = User.objects.get(phone_number=phone)
        except User.DoesNotExist:
            return auth_error('PHONE_NOT_REGISTERED', status.HTTP_404_NOT_FOUND)
        if user.is_banned:
            return auth_error('USER_BANNED', status.HTTP_403_FORBIDDEN)
        otp_result = consume_otp(user, OtpCode.PURPOSE_LOGIN, otp)
        otp_error = _otp_failure_response(otp_result)
        if otp_error is not None:
            return otp_error
        # mark verified if not
        if not user.is_verified:
            user.is_verified = True
            save_fields = ['is_verified']
        else:
            save_fields = []

        # ensure numeric unique_id exists
        if not user.unique_id:
            user.unique_id = generate_unique_numeric_id(10)
            if 'unique_id' not in save_fields:
                save_fields.append('unique_id')

        if save_fields:
            user.save(update_fields=save_fields)
        tokens = issue_tokens_for_user(user, request)
        from .serializers import UserSerializer
        user_data = UserSerializer(user, context={'request': request}).data
        return Response({'accessToken': tokens['accessToken'], 'refreshToken': tokens['refreshToken'], 'user': user_data})


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistAuthView(AuthAPIView):
    """Create / retrieve / update artist authentication submissions for the authenticated user."""
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="دریافت وضعیت احراز هویت هنرمند",
        description="دریافت اطلاعات و وضعیت فعلی درخواست احراز هویت هنرمند.",
        responses={200: ArtistAuthSerializer}
    )
    def get(self, request):
        if User.ROLE_ARTIST not in (request.user.roles or []):
            return auth_error('ARTIST_ONLY', status.HTTP_403_FORBIDDEN)
        try:
            auth = request.user.artist_auth
        except ObjectDoesNotExist:
            return auth_error('ARTIST_AUTH_NOT_FOUND', status.HTTP_404_NOT_FOUND)
        serializer = ArtistAuthSerializer(auth, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ثبت درخواست احراز هویت هنرمند",
        description="ارسال مدارک و اطلاعات لازم برای تایید حساب کاربری به عنوان هنرمند.",
        request=ArtistAuthSerializer,
        responses={201: ArtistAuthSerializer}
    )
    def post(self, request):
        if User.ROLE_ARTIST not in (request.user.roles or []):
            return auth_error('ARTIST_ONLY', status.HTTP_403_FORBIDDEN)
        if not request.user.is_verified:
            return auth_error('ACCOUNT_NOT_VERIFIED', status.HTTP_403_FORBIDDEN)
        # create or replace submission for this user
        if hasattr(request.user, 'artist_auth'):
            return auth_error('SUBMISSION_EXISTS', status.HTTP_409_CONFLICT)
        serializer = ArtistAuthSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        try:
            serializer.save(user=request.user)
        except MediaPipelineError as exc:
            return Response(
                {'error': str(exc), 'code': exc.code},
                status=exc.status_code,
            )
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="ویرایش درخواست احراز هویت هنرمند",
        description="به‌روزرسانی مدارک یا اطلاعات درخواست احراز هویت قبلی.",
        request=ArtistAuthSerializer,
        responses={200: ArtistAuthSerializer}
    )
    def patch(self, request):
        if User.ROLE_ARTIST not in (request.user.roles or []):
            return auth_error('ARTIST_ONLY', status.HTTP_403_FORBIDDEN)
        if not request.user.is_verified:
            return auth_error('ACCOUNT_NOT_VERIFIED', status.HTTP_403_FORBIDDEN)
        try:
            auth = request.user.artist_auth
        except ObjectDoesNotExist:
            return auth_error('ARTIST_AUTH_NOT_FOUND', status.HTTP_404_NOT_FOUND)
        serializer = ArtistAuthSerializer(auth, data=request.data, partial=True, context={'request': request})
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        try:
            serializer.save()
        except MediaPipelineError as exc:
            return Response(
                {'error': str(exc), 'code': exc.code},
                status=exc.status_code,
            )
        return Response(serializer.data)


@extend_schema(tags=['Artist Auth Endpoints'])
class ArtistVerificationOtpResendView(AuthAPIView):
    """Resend the account-verification OTP used only by the artist panel."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Resend artist account verification code",
        request=ForgotPasswordSerializer,
        responses={200: inline_serializer(
            name='ArtistVerificationOtpResendResponse',
            fields={
                'status': serializers.CharField(),
                'resendAfterSeconds': serializers.IntegerField(),
                'expiresInSeconds': serializers.IntegerField(),
            },
        )},
    )
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = normalize_phone(serializer.validated_data['phone'])
        user, error_response = _artist_account_for_password_reset(phone)
        if error_response is not None:
            return error_response
        retry_after = otp_rate_limit_retry_after(phone, OtpCode.PURPOSE_VERIFY, user)
        if retry_after:
            return auth_error(
                'RATE_LIMIT', status.HTTP_429_TOO_MANY_REQUESTS,
                retry_after_seconds=retry_after,
            )
        _, sent = create_and_send_otp(user, phone, OtpCode.PURPOSE_VERIFY)
        if not sent:
            return auth_error('SMS_FAILED', status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({
            'status': 'otp_sent',
            'resendAfterSeconds': max(1, int(getattr(settings, 'OTP_SEND_COOLDOWN_SECONDS', 60))),
            'expiresInSeconds': 300,
        })


@extend_schema(tags=['Artist Auth Endpoints'])
class ArtistForgotPasswordView(AuthAPIView):
    """Start an artist-password reset without changing the audience flow."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Request artist password reset code",
        request=ForgotPasswordSerializer,
        responses={200: inline_serializer(
            name='ArtistForgotPasswordResponse',
            fields={
                'status': serializers.CharField(),
                'resendAfterSeconds': serializers.IntegerField(),
                'expiresInSeconds': serializers.IntegerField(),
            },
        )},
    )
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = normalize_phone(serializer.validated_data['phone'])
        user, error_response = _artist_account_for_password_reset(phone)
        if error_response is not None:
            return error_response
        retry_after = otp_rate_limit_retry_after(phone, OtpCode.PURPOSE_ARTIST_RESET, user)
        if retry_after:
            return auth_error(
                'RATE_LIMIT', status.HTTP_429_TOO_MANY_REQUESTS,
                retry_after_seconds=retry_after,
            )
        _, sent = create_and_send_otp(user, phone, OtpCode.PURPOSE_ARTIST_RESET)
        if not sent:
            return auth_error('SMS_FAILED', status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({
            'status': 'otp_sent',
            'resendAfterSeconds': max(1, int(getattr(settings, 'OTP_SEND_COOLDOWN_SECONDS', 60))),
            'expiresInSeconds': 300,
        })


@extend_schema(tags=['Artist Auth Endpoints'])
class ArtistPasswordResetVerifyView(AuthAPIView):
    """Consume an artist reset OTP and exchange it for a short-lived reset token."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Verify artist password reset code",
        request=VerifySerializer,
        responses={200: inline_serializer(
            name='ArtistPasswordResetVerifyResponse',
            fields={
                'status': serializers.CharField(),
                'resetToken': serializers.CharField(),
                'expiresInSeconds': serializers.IntegerField(),
            },
        )},
    )
    def post(self, request):
        serializer = VerifySerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = normalize_phone(serializer.validated_data['phone'])
        user, error_response = _artist_account_for_password_reset(phone)
        if error_response is not None:
            return error_response
        otp_result = consume_otp(
            user,
            OtpCode.PURPOSE_ARTIST_RESET,
            serializer.validated_data['otp'],
        )
        otp_error = _otp_failure_response(otp_result)
        if otp_error is not None:
            return otp_error
        return Response({
            'status': 'verified',
            'resetToken': _artist_reset_token(user),
            'expiresInSeconds': _artist_reset_ttl_seconds(),
        })


@extend_schema(tags=['Artist Auth Endpoints'])
class ArtistPasswordResetView(AuthAPIView):
    """Reset only the artist password; audience credentials and sessions stay intact."""

    permission_classes = [AllowAny]

    @extend_schema(
        summary="Reset artist password",
        request=ArtistPasswordResetSerializer,
        responses={200: inline_serializer(
            name='ArtistPasswordResetResponse',
            fields={'status': serializers.CharField()},
        )},
    )
    def post(self, request):
        serializer = ArtistPasswordResetSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = normalize_phone(serializer.validated_data['phone'])
        try:
            payload = _load_artist_reset_token(serializer.validated_data['resetToken'])
        except SignatureExpired:
            return auth_error('ARTIST_RESET_TOKEN_EXPIRED', status.HTTP_401_UNAUTHORIZED)
        except (BadSignature, TypeError, ValueError):
            return auth_error('ARTIST_RESET_TOKEN_INVALID', status.HTTP_401_UNAUTHORIZED)

        if (
            payload.get('purpose') != OtpCode.PURPOSE_ARTIST_RESET
            or payload.get('phone') != phone
        ):
            return auth_error('ARTIST_RESET_TOKEN_INVALID', status.HTTP_401_UNAUTHORIZED)

        try:
            token_user_id = int(payload.get('sub'))
        except (TypeError, ValueError):
            return auth_error('ARTIST_RESET_TOKEN_INVALID', status.HTTP_401_UNAUTHORIZED)

        supplied_fingerprint = str(payload.get('password_fingerprint') or '')
        new_password = serializer.validated_data['newPassword']
        with transaction.atomic():
            user = User.objects.select_for_update().filter(
                pk=token_user_id,
                phone_number=phone,
            ).first()
            if user is None or (
                User.ROLE_ARTIST not in (user.roles or []) and not user.artist_password
            ):
                return auth_error('ARTIST_ACCOUNT_NOT_FOUND', status.HTTP_404_NOT_FOUND)
            if user.is_banned:
                return auth_error('USER_BANNED', status.HTTP_403_FORBIDDEN)

            expected_fingerprint = _artist_password_fingerprint(user)
            if not hmac.compare_digest(expected_fingerprint, supplied_fingerprint):
                return auth_error('ARTIST_RESET_TOKEN_USED', status.HTTP_409_CONFLICT)
            if user.check_artist_password(new_password):
                return validation_error({
                    'newPassword': [serializers.ErrorDetail(
                        'رمز عبور جدید باید با رمز عبور فعلی متفاوت باشد.',
                        code='password_unchanged',
                    )],
                })

            user.set_artist_password(new_password)
            user.failed_login_attempts = 0
            user.locked_until = None
            user.save(update_fields=['artist_password', 'failed_login_attempts', 'locked_until'])

        # Deliberately do not revoke shared refresh tokens: those sessions may
        # belong to the already-working audience app for the same user account.
        return Response({'status': 'artist_password_reset'})


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class ForgotPasswordView(AuthAPIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="درخواست بازیابی رمز عبور",
        description="ارسال کد تایید به شماره موبایل برای شروع فرآیند بازیابی رمز عبور.",
        request=ForgotPasswordSerializer,
        responses={
            200: inline_serializer(
                name='ForgotPasswordResponse',
                fields={'status': serializers.CharField()}
            )
        }
    )
    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = normalize_phone(serializer.validated_data['phone'])
        # Keep the OTP purpose same; client will indicate in reset whether it's for artist password
        try:
            user = User.objects.get(phone_number=phone)
        except User.DoesNotExist:
            return auth_error('PHONE_NOT_REGISTERED', status.HTTP_404_NOT_FOUND)
        if user.is_banned:
            return auth_error('USER_BANNED', status.HTTP_403_FORBIDDEN)
        retry_after = otp_rate_limit_retry_after(phone, OtpCode.PURPOSE_RESET, user)
        if retry_after:
            return auth_error('RATE_LIMIT', status.HTTP_429_TOO_MANY_REQUESTS, retry_after_seconds=retry_after)
        otp_obj, sent = create_and_send_otp(user, phone, OtpCode.PURPOSE_RESET)
        if sent:
            return Response({'status': 'otp_sent'}, status=status.HTTP_200_OK)
        return auth_error('SMS_FAILED', status.HTTP_503_SERVICE_UNAVAILABLE)


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class PasswordResetView(AuthAPIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="تغییر رمز عبور (بازیابی)",
        description="تنظیم رمز عبور جدید با استفاده از کد تایید ارسال شده.",
        request=PasswordResetSerializer,
        responses={
            200: inline_serializer(
                name='PasswordResetResponse',
                fields={'status': serializers.CharField()}
            )
        }
    )
    def post(self, request):
        serializer = PasswordResetSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        phone = serializer.validated_data.get('phone')
        otp = serializer.validated_data.get('otp')
        new_password = serializer.validated_data.get('newPassword')
        artist_flag = parse_artist_flag(request)
        admin_flag = bool(serializer.validated_data.get('admin_login', False))
        if phone:
            phone = normalize_phone(phone)
            try:
                user = User.objects.get(phone_number=phone)
            except User.DoesNotExist:
                return auth_error('PHONE_NOT_REGISTERED', status.HTTP_404_NOT_FOUND)
            if user.is_banned:
                return auth_error('USER_BANNED', status.HTTP_403_FORBIDDEN)
            otp_result = consume_otp(user, OtpCode.PURPOSE_RESET, otp or '')
            otp_error = _otp_failure_response(otp_result)
            if otp_error is not None:
                return otp_error
            # If client specified artist, reset artist password, otherwise reset main password
            if artist_flag:
                user.set_artist_password(new_password)
            else:
                user.set_password(new_password)
            # ensure unique_id exists after password reset if missing
            if not user.unique_id:
                user.unique_id = generate_unique_numeric_id(10)
            user.save()
            # revoke refresh tokens
            RefreshToken.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())
            return Response({'status': 'password_reset'})
        return auth_error('VALIDATION_ERROR', status.HTTP_400_BAD_REQUEST, fields={'phone': ['این فیلد الزامی است.']})


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class TokenRefreshView(AuthAPIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="تجدید توکن دسترسی",
        description="دریافت توکن دسترسی جدید با استفاده از توکن تجدید (Refresh Token).",
        request=TokenRefreshRequestSerializer,
        responses={
            200: inline_serializer(
                name='TokenRefreshResponse',
                fields={
                    'accessToken': serializers.CharField(),
                    'refreshToken': serializers.CharField(),
                    'user': __import__('api.serializers', fromlist=['']).UserSerializer,
                }
            )
        }
    )
    def post(self, request):
        serializer = TokenRefreshRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        refresh_token = serializer.validated_data['refreshToken']
        # Validate the JWT first. Only malformed/expired JWTs are reported as
        # TOKEN_INVALID; database failures are allowed to surface as SERVER_ERROR.
        try:
            rt = SimpleRefreshToken(refresh_token)
            user_id = rt['user_id']
        except (TokenError, KeyError, TypeError, ValueError):
            return auth_error('TOKEN_INVALID', status.HTTP_401_UNAUTHORIZED)

        user = User.objects.filter(id=user_id).first()
        if user is None:
            return auth_error('TOKEN_INVALID', status.HTTP_401_UNAUTHORIZED)
        if user.is_banned:
            return auth_error('USER_BANNED', status.HTTP_403_FORBIDDEN)
        if not user.is_active:
            return auth_error('AUTH_FAILED', status.HTTP_401_UNAUTHORIZED)
        if is_employee(user):
            try:
                token_version = int(rt.get('admin_session_version', 0))
            except (TypeError, ValueError):
                token_version = 0
            if token_version != employee_session_version(user):
                return auth_error('TOKEN_REVOKED', status.HTTP_401_UNAUTHORIZED)

        # HMAC hashes cannot be queried by the raw token, so inspect only active,
        # unexpired sessions and retain the matching primary key.
        active_sessions = RefreshToken.objects.filter(
            user=user,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).only('id', 'token_hash')
        valid_session_id = next((
            session.id
            for session in active_sessions
            if check_refresh_token(refresh_token, session.token_hash)
        ), None)
        if valid_session_id is None:
            return auth_error('TOKEN_REVOKED', status.HTTP_401_UNAUTHORIZED)

        # Serialize refresh-token rotation at the database row. A concurrent
        # request that arrives with the old token loses the re-check and receives
        # TOKEN_REVOKED instead of issuing a second valid refresh token.
        with transaction.atomic():
            valid_session = RefreshToken.objects.select_for_update().filter(
                id=valid_session_id,
                user=user,
                revoked_at__isnull=True,
                expires_at__gt=timezone.now(),
            ).first()
            if valid_session is None or not check_refresh_token(
                refresh_token, valid_session.token_hash
            ):
                return auth_error('TOKEN_REVOKED', status.HTTP_401_UNAUTHORIZED)

            new_refresh = SimpleRefreshToken.for_user(user)
            if is_employee(user):
                new_refresh['admin_session_version'] = employee_session_version(user)
            new_access = new_refresh.access_token
            device_name, device_type, os_info = get_device_info(request)
            valid_session.token_hash = hash_refresh_token(str(new_refresh))
            valid_session.expires_at = timezone.now() + timedelta(days=30)
            valid_session.user_agent = request.META.get('HTTP_USER_AGENT', '')
            valid_session.ip = request.META.get('REMOTE_ADDR', '')
            valid_session.device_name = device_name
            valid_session.device_type = device_type
            valid_session.os_info = os_info
            valid_session.save(update_fields=[
                'token_hash', 'expires_at', 'user_agent', 'ip',
                'device_name', 'device_type', 'os_info',
            ])

        from .serializers import UserSerializer
        user_data = UserSerializer(user, context={'request': request}).data
        return Response({
            'accessToken': str(new_access),
            'refreshToken': str(new_refresh),
            'user': user_data
        })


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class LogoutView(AuthAPIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="خروج از حساب کاربری",
        description="ابطال توکن تجدید و خروج از حساب کاربری.",
        request=LogoutSerializer,
        responses={
            200: inline_serializer(
                name='LogoutResponse',
                fields={'status': serializers.CharField()}
            )
        }
    )
    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        refresh_token = serializer.validated_data['refreshToken']
        # Logout is idempotent and does not reveal whether a token was valid.
        # Revoke only this device session; revoking other devices is a separate action.
        try:
            rt = SimpleRefreshToken(refresh_token)
            user_id = rt['user_id']
        except (TokenError, KeyError, TypeError, ValueError):
            return Response({'status': 'ok'})

        sessions = RefreshToken.objects.filter(
            user_id=user_id,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).only('id', 'token_hash')
        matching_id = next((
            session.id for session in sessions
            if check_refresh_token(refresh_token, session.token_hash)
        ), None)
        if matching_id is not None:
            RefreshToken.objects.filter(
                pk=matching_id, revoked_at__isnull=True,
            ).update(revoked_at=timezone.now())
        return Response({'status': 'ok'})


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class SessionListView(AuthAPIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لیست نشست‌های فعال",
        description="دریافت لیست تمامی دستگاه‌ها و نشست‌های فعال کاربر.",
        parameters=[
            OpenApiParameter("refreshToken", OpenApiTypes.STR, description="توکن فعلی برای تشخیص نشست جاری")
        ],
        responses={200: SessionSerializer(many=True)}
    )
    def get(self, request):
        sessions = RefreshToken.objects.filter(
            user=request.user, 
            revoked_at__isnull=True, 
            expires_at__gt=timezone.now()
        ).order_by('-created_at')
        
        # If the client provides their current refreshToken, we can mark it as current
        current_token = request.query_params.get('refreshToken')
        
        serializer = SessionSerializer(sessions, many=True, context={'request': request, 'current_token': current_token})
        return Response(serializer.data)


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class SessionRevokeView(AuthAPIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="ابطال نشست خاص",
        description="خروج از حساب کاربری در یک دستگاه خاص.",
        responses={
            200: inline_serializer(
                name='SessionRevokeResponse',
                fields={'status': serializers.CharField()}
            )
        }
    )
    def post(self, request, pk):
        session = RefreshToken.objects.filter(pk=pk, user=request.user).first()
        if session is None:
            return auth_error('SESSION_NOT_FOUND', status.HTTP_404_NOT_FOUND)
        session.revoked_at = timezone.now()
        session.save(update_fields=['revoked_at'])
        return Response({'status': 'ok'})


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class SessionRevokeOtherView(AuthAPIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="ابطال سایر نشست‌ها",
        description="خروج از حساب کاربری در تمامی دستگاه‌ها به جز دستگاه فعلی.",
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'refreshToken': {'type': 'string'}
                },
                'required': ['refreshToken']
            }
        },
        responses={
            200: inline_serializer(
                name='SessionRevokeOtherResponse',
                fields={
                    'status': serializers.CharField(),
                    'revoked_count': serializers.IntegerField(),
                }
            )
        }
    )
    def post(self, request):
        current_refresh = request.data.get('refreshToken')
        if not current_refresh:
            return auth_error(
                'REFRESH_TOKEN_REQUIRED',
                status.HTTP_400_BAD_REQUEST,
                fields={
                    'refreshToken': [serializers.ErrorDetail(
                        'این فیلد الزامی است.', code='required'
                    )]
                },
            )

        sessions = list(RefreshToken.objects.filter(
            user=request.user,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).only('id', 'token_hash'))
        current_session_id = next((
            session.id for session in sessions
            if check_refresh_token(current_refresh, session.token_hash)
        ), None)
        if current_session_id is None:
            # A stale or foreign token must never cause every valid session to
            # be revoked. Return an explicit, recoverable session error.
            return auth_error('CURRENT_SESSION_INVALID', status.HTTP_401_UNAUTHORIZED)

        revoked_count = RefreshToken.objects.filter(
            user=request.user,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).exclude(pk=current_session_id).update(revoked_at=timezone.now())

        return Response({'status': 'ok', 'revoked_count': revoked_count})


@extend_schema(tags=['Auth Endpoints اندپوینت های احراز'])
class ChangePasswordView(AuthAPIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="تغییر رمز عبور",
        description="تغییر رمز عبور فعلی به رمز عبور جدید.",
        request=ChangePasswordSerializer,
        responses={
            200: inline_serializer(
                name='ChangePasswordResponse',
                fields={
                    'status': serializers.CharField(),
                    'message': serializers.CharField(),
                }
            )
        }
    )
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return validation_error(serializer.errors)
        
        current_password = serializer.validated_data['currentPassword']
        new_password = serializer.validated_data['newPassword']
        artist_flag = parse_artist_flag(request)
        admin_flag = bool(serializer.validated_data.get('admin_login', False))
        user = request.user

        # Validate with the correct password type
        if artist_flag:
            if not user.check_artist_password(current_password):
                return auth_error('INVALID_PASSWORD', status.HTTP_400_BAD_REQUEST, fields={'currentPassword': [serializers.ErrorDetail('رمز عبور فعلی صحیح نیست.', code='invalid_password')]})
            user.set_artist_password(new_password)
        else:
            if not user.check_password(current_password):
                return auth_error('INVALID_PASSWORD', status.HTTP_400_BAD_REQUEST, fields={'currentPassword': [serializers.ErrorDetail('رمز عبور فعلی صحیح نیست.', code='invalid_password')]})
            user.set_password(new_password)
        user.save()
        
        # Revoke all other sessions except the current one
        device_name, device_type, os_info = get_device_info(request)
        ua = request.META.get('HTTP_USER_AGENT', '')
        ip = request.META.get('REMOTE_ADDR', '')

        RefreshToken.objects.filter(user=user, revoked_at__isnull=True).exclude(
            user_agent=ua,
            ip=ip,
            device_name=device_name,
            device_type=device_type,
            os_info=os_info
        ).update(revoked_at=timezone.now())
        
        return Response({'status': 'ok', 'message': 'رمز عبور با موفقیت تغییر کرد.'})

