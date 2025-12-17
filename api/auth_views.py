from django.utils import timezone
from django.conf import settings
from django.shortcuts import get_object_or_404
import logging
import requests
from django.contrib.auth.hashers import make_password, check_password
from django.db import transaction
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from .models import User, OtpCode, RefreshToken
from .serializers import (
    RegisterRequestSerializer,
    VerifySerializer,
    LoginPasswordSerializer,
    LoginOtpRequestSerializer,
    LoginOtpVerifySerializer,
    ForgotPasswordSerializer,
    PasswordResetSerializer,
    TokenRefreshRequestSerializer,
    LogoutSerializer,
)
from rest_framework_simplejwt.tokens import RefreshToken as SimpleRefreshToken
from django.utils.crypto import get_random_string
from datetime import timedelta
import hashlib


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


def hash_code(code: str) -> str:
    # use Django's make_password for salted hash
    return make_password(code)


def check_code_hash(raw: str, hashed: str) -> bool:
    return check_password(raw, hashed)


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
    }

    template_name = template_map.get(purpose, 'login')

    # Ensure phone is in local format (09xxxxxxxxx)
    receptor = normalize_phone(phone)

    url = f"https://api.kavenegar.com/v1/{api_key}/verify/lookup.json"
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    data = {
        'receptor': receptor,
        'token': code,
        'template': template_name,
    }

    try:
        resp = requests.post(url, data=data, headers=headers, timeout=5)
        if resp.status_code != 200:
            logger.error('Kavenegar returned non-200 status: %s %s', resp.status_code, resp.text)
            return False
        j = resp.json()
        logger.info('Kavenegar sent SMS to %s (template=%s): %s', receptor, template_name, j)
        return True
    except Exception as e:
        logger.exception('Error sending SMS via Kavenegar: %s', e)
        return False


def create_and_send_otp(user: User or None, phone: str, purpose: str, minutes=5) -> OtpCode:
    otp = generate_otp(4)
    hashed = hash_code(otp)
    expires = timezone.now() + timedelta(minutes=minutes)
    otp_obj = OtpCode.objects.create(user=user, code_hash=hashed, purpose=purpose, expires_at=expires)
    # send SMS and log result to help debugging in development
    sent = send_sms(phone, otp, purpose, minutes)
    logger = logging.getLogger(__name__)
    if sent:
        logger.info("OTP created and SMS send attempt succeeded for phone=%s purpose=%s", phone, purpose)
    else:
        logger.warning("OTP created but SMS send attempt failed for phone=%s purpose=%s", phone, purpose)
    return otp_obj, sent


def issue_tokens_for_user(user: User, request) -> dict:
    refresh = SimpleRefreshToken.for_user(user)
    access = refresh.access_token
    # persist hashed refresh token for revocation / rotation tracking
    token_str = str(refresh)
    token_hash = make_password(token_str)
    expires_at = timezone.now() + timedelta(days=30)
    RefreshToken.objects.create(user=user, token_hash=token_hash, user_agent=request.META.get('HTTP_USER_AGENT', ''), ip=request.META.get('REMOTE_ADDR', ''), expires_at=expires_at)
    # update last_login
    user.last_login_at = timezone.now()
    user.failed_login_attempts = 0
    user.save(update_fields=['last_login_at', 'failed_login_attempts'])
    return {'accessToken': str(access), 'refreshToken': token_str}


class AuthRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        phone = normalize_phone(serializer.validated_data['phone'])
        password = serializer.validated_data['password']
        # If user exists
        existing = User.objects.filter(phone_number=phone).first()
        if existing:
            # If already verified, block registration
            if existing.is_verified:
                return Response({'error': {'code': 'USER_EXISTS', 'message': 'Phone already registered'}}, status=status.HTTP_409_CONFLICT)
            # Not verified: allow resend but rate-limit to 1 minute since last verify OTP
            last_otp = OtpCode.objects.filter(user=existing, purpose=OtpCode.PURPOSE_VERIFY).order_by('-created_at').first()
            if last_otp:
                elapsed = timezone.now() - last_otp.created_at
                if elapsed < timedelta(minutes=1):
                    # Too soon to resend
                    retry_after = int((timedelta(minutes=1) - elapsed).total_seconds())
                    return Response({'error': {'code': 'RATE_LIMIT', 'message': 'Please wait before requesting another OTP', 'retry_after_seconds': retry_after}}, status=status.HTTP_429_TOO_MANY_REQUESTS)
            # send new OTP to existing unverified user
            otp_obj, sent = create_and_send_otp(existing, phone, OtpCode.PURPOSE_VERIFY)
            if sent:
                return Response({'status': 'ok', 'message': 'OTP sent'}, status=status.HTTP_200_OK)
            return Response({'error': {'code': 'SMS_FAILED', 'message': 'Failed to send OTP SMS'}}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # create user with is_verified False
        user = User.objects.create_user(phone_number=phone, password=password)
        user.is_verified = False
        user.save(update_fields=['is_verified'])
        # create OTP and attempt to send SMS
        otp_obj, sent = create_and_send_otp(user, phone, OtpCode.PURPOSE_VERIFY)
        if sent:
            return Response({'status': 'ok', 'message': 'OTP sent'}, status=status.HTTP_200_OK)
        # SMS failed: return error with details
        return Response({'error': {'code': 'SMS_FAILED', 'message': 'Failed to send OTP SMS'}}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AuthVerifyView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = VerifySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        phone = normalize_phone(serializer.validated_data['phone'])
        otp = serializer.validated_data['otp']
        purpose = OtpCode.PURPOSE_VERIFY
        user = get_object_or_404(User, phone_number=phone)
        # find latest unconsumed otp
        otp_qs = OtpCode.objects.filter(user=user, purpose=purpose, consumed=False, expires_at__gt=timezone.now()).order_by('-created_at')
        if not otp_qs.exists():
            return Response({'error': {'code': 'OTP_NOT_FOUND', 'message': 'No valid OTP found'}}, status=status.HTTP_401_UNAUTHORIZED)
        otp_obj = otp_qs.first()
        if otp_obj.attempts >= 3:
            otp_obj.consumed = True
            otp_obj.save(update_fields=['consumed'])
            return Response({'error': {'code': 'OTP_EXCEEDED', 'message': 'OTP attempts exceeded'}}, status=status.HTTP_401_UNAUTHORIZED)
        if not check_code_hash(otp, otp_obj.code_hash):
            otp_obj.attempts += 1
            otp_obj.save(update_fields=['attempts'])
            return Response({'error': {'code': 'OTP_INVALID', 'message': 'The provided OTP is invalid.'}}, status=status.HTTP_401_UNAUTHORIZED)
        # success
        otp_obj.consumed = True
        otp_obj.save(update_fields=['consumed'])
        user.is_verified = True
        user.save(update_fields=['is_verified'])
        tokens = issue_tokens_for_user(user, request)
        return Response({'accessToken': tokens['accessToken'], 'refreshToken': tokens['refreshToken'], 'user': {'id': user.id, 'phone': user.phone_number, 'is_verified': user.is_verified}})


class LoginPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        phone = normalize_phone(serializer.validated_data['phone'])
        password = serializer.validated_data['password']
        try:
            user = User.objects.get(phone_number=phone)
        except User.DoesNotExist:
            return Response({'error': {'code': 'AUTH_FAILED', 'message': 'Invalid credentials'}}, status=status.HTTP_401_UNAUTHORIZED)
        # lockout check
        if user.locked_until and user.locked_until > timezone.now():
            return Response({'error': {'code': 'ACCOUNT_LOCKED', 'message': 'Account temporarily locked'}}, status=status.HTTP_403_FORBIDDEN)
        if not user.check_password(password):
            user.failed_login_attempts = (user.failed_login_attempts or 0) + 1
            if user.failed_login_attempts >= 5:
                user.locked_until = timezone.now() + timedelta(minutes=15)
            user.save(update_fields=['failed_login_attempts', 'locked_until'])
            return Response({'error': {'code': 'AUTH_FAILED', 'message': 'Invalid credentials'}}, status=status.HTTP_401_UNAUTHORIZED)
        # success
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = timezone.now()
        user.save(update_fields=['failed_login_attempts', 'locked_until', 'last_login_at'])
        tokens = issue_tokens_for_user(user, request)
        return Response({'accessToken': tokens['accessToken'], 'refreshToken': tokens['refreshToken'], 'user': {'id': user.id, 'phone': user.phone_number, 'is_verified': user.is_verified}})


class LoginOtpRequestView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginOtpRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        phone = normalize_phone(serializer.validated_data['phone'])
        try:
            user = User.objects.get(phone_number=phone)
        except User.DoesNotExist:
            return Response({'error': {'code': 'NOT_FOUND', 'message': 'Phone not registered'}}, status=status.HTTP_404_NOT_FOUND)
        # rate limit: max 3 sends in last hour
        one_hour_ago = timezone.now() - timedelta(hours=1)
        sends = OtpCode.objects.filter(user=user, created_at__gte=one_hour_ago).count()
        if sends >= 3:
            return Response({'error': {'code': 'RATE_LIMIT', 'message': 'Too many OTP requests'}}, status=status.HTTP_429_TOO_MANY_REQUESTS)
        otp_obj, sent = create_and_send_otp(user, phone, OtpCode.PURPOSE_LOGIN)
        if sent:
            return Response({'status': 'otp_sent'}, status=status.HTTP_200_OK)
        return Response({'error': {'code': 'SMS_FAILED', 'message': 'Failed to send OTP SMS'}}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class LoginOtpVerifyView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginOtpVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        phone = normalize_phone(serializer.validated_data['phone'])
        otp = serializer.validated_data['otp']
        try:
            user = User.objects.get(phone_number=phone)
        except User.DoesNotExist:
            return Response({'error': {'code': 'NOT_FOUND', 'message': 'Phone not registered'}}, status=status.HTTP_404_NOT_FOUND)
        otp_qs = OtpCode.objects.filter(user=user, purpose=OtpCode.PURPOSE_LOGIN, consumed=False, expires_at__gt=timezone.now()).order_by('-created_at')
        if not otp_qs.exists():
            return Response({'error': {'code': 'OTP_NOT_FOUND', 'message': 'No valid OTP found'}}, status=status.HTTP_401_UNAUTHORIZED)
        otp_obj = otp_qs.first()
        if otp_obj.attempts >= 3:
            otp_obj.consumed = True
            otp_obj.save(update_fields=['consumed'])
            return Response({'error': {'code': 'OTP_EXCEEDED', 'message': 'OTP attempts exceeded'}}, status=status.HTTP_401_UNAUTHORIZED)
        if not check_code_hash(otp, otp_obj.code_hash):
            otp_obj.attempts += 1
            otp_obj.save(update_fields=['attempts'])
            return Response({'error': {'code': 'OTP_INVALID', 'message': 'The provided OTP is invalid.'}}, status=status.HTTP_401_UNAUTHORIZED)
        otp_obj.consumed = True
        otp_obj.save(update_fields=['consumed'])
        # mark verified if not
        if not user.is_verified:
            user.is_verified = True
            user.save(update_fields=['is_verified'])
        tokens = issue_tokens_for_user(user, request)
        return Response({'accessToken': tokens['accessToken'], 'refreshToken': tokens['refreshToken'], 'user': {'id': user.id, 'phone': user.phone_number, 'is_verified': user.is_verified}})


class ForgotPasswordView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ForgotPasswordSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        phone = normalize_phone(serializer.validated_data['phone'])
        try:
            user = User.objects.get(phone_number=phone)
        except User.DoesNotExist:
            return Response({'error': {'code': 'NOT_FOUND', 'message': 'Phone not registered'}}, status=status.HTTP_404_NOT_FOUND)
        otp_obj, sent = create_and_send_otp(user, phone, OtpCode.PURPOSE_RESET)
        if sent:
            return Response({'status': 'otp_sent'}, status=status.HTTP_200_OK)
        return Response({'error': {'code': 'SMS_FAILED', 'message': 'Failed to send OTP SMS'}}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class PasswordResetView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = PasswordResetSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        phone = serializer.validated_data.get('phone')
        otp = serializer.validated_data.get('otp')
        new_password = serializer.validated_data.get('newPassword')
        if phone:
            phone = normalize_phone(phone)
            try:
                user = User.objects.get(phone_number=phone)
            except User.DoesNotExist:
                return Response({'error': {'code': 'NOT_FOUND', 'message': 'Phone not registered'}}, status=status.HTTP_404_NOT_FOUND)
            otp_qs = OtpCode.objects.filter(user=user, purpose=OtpCode.PURPOSE_RESET, consumed=False, expires_at__gt=timezone.now()).order_by('-created_at')
            if not otp_qs.exists():
                return Response({'error': {'code': 'OTP_NOT_FOUND', 'message': 'No valid OTP found'}}, status=status.HTTP_401_UNAUTHORIZED)
            otp_obj = otp_qs.first()
            if not otp or not check_code_hash(otp, otp_obj.code_hash):
                otp_obj.attempts += 1
                otp_obj.save(update_fields=['attempts'])
                return Response({'error': {'code': 'OTP_INVALID', 'message': 'The provided OTP is invalid.'}}, status=status.HTTP_401_UNAUTHORIZED)
            # valid
            otp_obj.consumed = True
            otp_obj.save(update_fields=['consumed'])
            user.set_password(new_password)
            user.save()
            # revoke refresh tokens
            RefreshToken.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())
            return Response({'status': 'password_reset'})
        return Response({'error': {'code': 'BAD_REQUEST', 'message': 'phone is required'}}, status=status.HTTP_400_BAD_REQUEST)


class TokenRefreshView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = TokenRefreshRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        refresh_token = serializer.validated_data['refreshToken']
        # validate token using SimpleJWT
        try:
            rt = SimpleRefreshToken(refresh_token)
            user_id = rt['user_id']
            user = User.objects.get(id=user_id)
        except Exception:
            return Response({'error': {'code': 'TOKEN_INVALID', 'message': 'Invalid refresh token'}}, status=status.HTTP_401_UNAUTHORIZED)
        # Optionally check DB stored hashed token exists (best-effort)
        # rotate: create new refresh and store
        new_refresh = SimpleRefreshToken.for_user(user)
        new_access = new_refresh.access_token
        # store new refresh hashed and revoke old ones matching token string
        try:
            # revoke matching stored token(s)
            hashed = make_password(refresh_token)
            RefreshToken.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())
            RefreshToken.objects.create(user=user, token_hash=make_password(str(new_refresh)), user_agent=request.META.get('HTTP_USER_AGENT', ''), ip=request.META.get('REMOTE_ADDR', ''), expires_at=timezone.now()+timedelta(days=30))
        except Exception:
            pass
        return Response({'accessToken': str(new_access), 'refreshToken': str(new_refresh)})


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        refresh_token = serializer.validated_data['refreshToken']
        # revoke matching RefreshToken entries
        try:
            # best-effort: mark all tokens for user as revoked if token is valid
            rt = SimpleRefreshToken(refresh_token)
            user_id = rt['user_id']
            RefreshToken.objects.filter(user_id=user_id, revoked_at__isnull=True).update(revoked_at=timezone.now())
        except Exception:
            # if token invalid, still return success to avoid token probing
            pass
        return Response({'status': 'ok'})
