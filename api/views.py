from rest_framework import generics, permissions, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import action
import re
from .models import (
    User, Artist, Album, Playlist,NotificationSetting, Genre, Mood, Tag, SubGenre, Song, 
    StreamAccess, PlayCount, UserPlaylist, RecommendedPlaylist, EventPlaylist, SearchSection,
    ArtistMonthlyListener, UserHistory, Follow, SongLike, AlbumLike, PlaylistLike, Rules, PlayConfiguration,
    ActivePlayback, DepositRequest, Report, Notification, AudioAd
)
from .serializers import (
    UserSerializer,PlaylistSerializer,NotificationSettingSerializer,
    RegisterSerializer, 
    CustomTokenObtainPairSerializer,
    ArtistSerializer,
    PopularArtistSerializer,
    AlbumSerializer,
    PopularAlbumSerializer,
    GenreSerializer,
    MoodSerializer,
    TagSerializer,
    SubGenreSerializer,
    SongSerializer,
    SongUploadSerializer,
    UploadSerializer,
    SongStreamSerializer,
    UserPlaylistSerializer,
    UserPlaylistCreateSerializer,
    RecommendedPlaylistListSerializer,
    RecommendedPlaylistDetailSerializer,
    SearchResultSerializer,
    EventPlaylistSerializer,
    SearchSectionSerializer,
    FollowRequestSerializer,
    LikedSongSerializer,
    LikedAlbumSerializer,
    LikedPlaylistSerializer,
    RulesSerializer,
    DepositRequestSerializer,
    ReportSerializer,
    NotificationSerializer,
    AudioAdSerializer,
    SongSummarySerializer,
    ArtistSummarySerializer,
    AlbumSummarySerializer,
    PlaylistSummarySerializer,
)
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.pagination import PageNumberPagination
from django.db.models import Sum, Count, F, IntegerField, Value, Prefetch, DecimalField
from django.db.models.functions import Coalesce, TruncDate, TruncHour, TruncWeek, TruncMonth
from django.utils import timezone
from django.conf import settings
from .utils import (
    upload_file_to_r2, generate_signed_r2_url, 
    get_audio_info, convert_to_128kbps
)
from .auth_views import normalize_phone, create_and_send_otp, OtpCode
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import uuid
import os
import mimetypes
import random
import time
import secrets
from mutagen.mp3 import MP3
from mutagen.wave import WAVE
from django.utils import timezone
from datetime import timedelta
from django.db.models import Q, Count, Avg, F
from django.shortcuts import get_object_or_404
from django.urls import reverse
from rest_framework import serializers
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, inline_serializer


# Filename helpers
def get_artist_display_name_from_user(user):
    """Return the artist's display name (stage name if present, otherwise artist name) for a given user.
    Returns None if no artist profile is attached.
    """
    try:
        if hasattr(user, 'artist_profile') and user.artist_profile:
            art = user.artist_profile
            return art.artistic_name or art.name
    except Exception:
        pass
    return None


def make_safe_filename(s: str) -> str:
    """Sanitize a filename base by removing problematic characters and collapsing whitespace."""
    if not s:
        return ''
    # Allow basic punctuation that is common in music filenames
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_.,()')
    cleaned = ''.join(ch for ch in s if ch in allowed)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


class RegisterView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="ثبت‌نام کاربر جدید",
        description="ایجاد یک حساب کاربری جدید با استفاده از شماره موبایل و رمز عبور.",
        request=RegisterSerializer,
        responses={200: UserSerializer}
    )
    def post(self, request, *args, **kwargs):
        # If client requests artist-only flow: accept only phone and artistPassword,
        # add or create user with artist role and send verification OTP even if already registered.
        if request.data.get('artist'):
            phone_raw = request.data.get('phone')
            artist_password = request.data.get('artistPassword')
            phone = normalize_phone(phone_raw or '')
            if not phone:
                return Response({'error': 'phone is required'}, status=status.HTTP_400_BAD_REQUEST)

            existing = User.objects.filter(phone_number=phone).first()
            if existing:
                if existing.is_banned:
                    return Response({'error': {'code': 'USER_BANNED', 'message': 'This account has been banned.'}}, status=status.HTTP_403_FORBIDDEN)
                # ensure artist role present
                if User.ROLE_ARTIST not in (existing.roles or []):
                    existing.roles = (existing.roles or []) + [User.ROLE_ARTIST]
                if artist_password:
                    existing.set_artist_password(artist_password)
                existing.save()
                otp_obj, sent = create_and_send_otp(existing, phone, OtpCode.PURPOSE_VERIFY)
                if sent:
                    return Response({'status': 'ok', 'message': 'OTP sent'}, status=status.HTTP_200_OK)
                return Response({'error': {'code': 'SMS_FAILED', 'message': 'Failed to send OTP SMS'}}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # create a new user with artist role (no main password required here)
            create_kwargs = {'roles': [User.ROLE_AUDIENCE, User.ROLE_ARTIST]}
            if artist_password:
                create_kwargs['artist_password'] = artist_password
            user = User.objects.create_user(phone_number=phone, password=None, **create_kwargs)
            user.is_verified = False
            user.save(update_fields=['is_verified'])
            otp_obj, sent = create_and_send_otp(user, phone, OtpCode.PURPOSE_VERIFY)
            if sent:
                return Response({'status': 'ok', 'message': 'OTP sent'}, status=status.HTTP_200_OK)
            return Response({'error': {'code': 'SMS_FAILED', 'message': 'Failed to send OTP SMS'}}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # default full registration flow
        serializer = RegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()
            return Response(UserSerializer(user).data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CustomTokenObtainPairView(TokenObtainPairView):
    permission_classes = [AllowAny]
    serializer_class = CustomTokenObtainPairSerializer

    @extend_schema(
        summary="ورود و دریافت توکن",
        description="دریافت توکن‌های Access و Refresh با استفاده از شماره موبایل و رمز عبور.",
        responses={
            200: inline_serializer(
                name='TokenObtainResponse',
                fields={
                    'access': serializers.CharField(),
                    'refresh': serializers.CharField(),
                    'user': UserSerializer(),
                }
            )
        }
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


class CustomTokenRefreshView(TokenRefreshView):
    # uses SimpleJWT's TokenRefreshView; with ROTATE_REFRESH_TOKENS=True it will return a new refresh token too
    permission_classes = [AllowAny]

    @extend_schema(
        summary="تمدید توکن",
        description="دریافت توکن Access جدید با استفاده از توکن Refresh.",
        responses={
            200: inline_serializer(
                name='TokenRefreshResponse',
                fields={
                    'access': serializers.CharField(),
                    'refresh': serializers.CharField(),
                }
            )
        }
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class UserProfileView(APIView):
    """Retrieve and Update User Profile"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="مشاهده پروفایل کاربر",
        description="دریافت اطلاعات پروفایل کاربر فعلی.",
        responses={200: UserSerializer}
    )
    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش پروفایل کاربر",
        description="به‌روزرسانی اطلاعات پروفایل کاربر فعلی.",
        request=UserSerializer,
        responses={200: UserSerializer}
    )
    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class NotificationSettingUpdateView(APIView):
    """Update User Notification Settings"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="دریافت تنظیمات اعلان‌ها",
        description="مشاهده تنظیمات فعلی اعلان‌های کاربر.",
        responses={200: NotificationSettingSerializer}
    )
    def get(self, request):
        setting, created = NotificationSetting.objects.get_or_create(user=request.user)
        serializer = NotificationSettingSerializer(setting)
        return Response(serializer.data)

    @extend_schema(
        summary="به‌روزرسانی تنظیمات اعلان‌ها (کامل)",
        description="تغییر تمامی تنظیمات اعلان‌های کاربر.",
        request=NotificationSettingSerializer,
        responses={200: NotificationSettingSerializer}
    )
    def put(self, request):
        setting, created = NotificationSetting.objects.get_or_create(user=request.user)
        serializer = NotificationSettingSerializer(setting, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="به‌روزرسانی تنظیمات اعلان‌ها (جزئی)",
        description="تغییر برخی از تنظیمات اعلان‌های کاربر.",
        request=NotificationSettingSerializer,
        responses={200: NotificationSettingSerializer}
    )
    def patch(self, request):
        setting, created = NotificationSetting.objects.get_or_create(user=request.user)
        serializer = NotificationSettingSerializer(setting, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class StreamQualityUpdateView(APIView):
    """Update User Stream Quality Settings"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="دریافت تنظیمات کیفیت پخش",
        description="مشاهده کیفیت پخش فعلی و نوع اشتراک کاربر.",
        responses={
            200: inline_serializer(
                name='StreamQualityResponse',
                fields={
                    'stream_quality': serializers.CharField(),
                    'plan': serializers.CharField(),
                }
            )
        }
    )
    def get(self, request):
        return Response({
            "stream_quality": request.user.stream_quality,
            "plan": request.user.plan
        })

    @extend_schema(
        summary="تغییر کیفیت پخش",
        description="تنظیم کیفیت پخش موسیقی (معمولی یا بالا). کیفیت بالا مخصوص کاربران ویژه است.",
        request=inline_serializer(
            name='StreamQualityUpdate',
            fields={
                'stream_quality': serializers.ChoiceField(choices=['medium', 'high'])
            }
        ),
        responses={
            200: inline_serializer(
                name='StreamQualityUpdateResponse',
                fields={
                    'stream_quality': serializers.CharField(),
                }
            )
        }
    )
    def put(self, request):
        quality = request.data.get('stream_quality')
        if quality not in ['medium', 'high']:
            return Response({"detail": "Invalid quality choice."}, status=status.HTTP_400_BAD_REQUEST)
        
        if quality == 'high' and request.user.plan != 'premium':
            return Response({"detail": "High quality streaming is only available for premium users."}, status=status.HTTP_403_FORBIDDEN)
        
        request.user.stream_quality = quality
        request.user.save(update_fields=['stream_quality'])
        return Response({"stream_quality": request.user.stream_quality})

    @extend_schema(
        summary="تغییر کیفیت پخش (جزئی)",
        description="تنظیم کیفیت پخش موسیقی.",
        request=inline_serializer(
            name='StreamQualityPatch',
            fields={
                'stream_quality': serializers.ChoiceField(choices=['medium', 'high'])
            }
        ),
        responses={
            200: inline_serializer(
                name='StreamQualityPatchResponse',
                fields={
                    'stream_quality': serializers.CharField(),
                }
            )
        }
    )
    def patch(self, request):
        return self.put(request)


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و  صفحات جزئیات و عملیات'])
class UserFollowView(APIView):
    """Follow or Unfollow a User or Artist"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="دنبال کردن یا لغو دنبال کردن",
        description="دنبال کردن یک کاربر یا هنرمند. اگر قبلاً دنبال شده باشد، لغو می‌شود.",
        request=FollowRequestSerializer,
        responses={
            200: inline_serializer(
                name='FollowResponse',
                fields={
                    'status': serializers.CharField(),
                    'message': serializers.CharField(),
                }
            )
        }
    )
    def post(self, request):
        serializer = FollowRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        user_id = serializer.validated_data.get('user_id')
        artist_id = serializer.validated_data.get('artist_id')
        
        follower = request.user
        # If the user has an artist profile, we could potentially follow as an artist.
        # For now, we follow as the User account as per "users only can post to it".
        # But we'll check if they want to follow as artist if we add that later.
        
        if user_id:
            target = get_object_or_404(User, id=user_id)
            if target == follower:
                return Response({'error': 'You cannot follow yourself.'}, status=status.HTTP_400_BAD_REQUEST)
            
            follow_qs = Follow.objects.filter(follower_user=follower, followed_user=target)
            if follow_qs.exists():
                follow_qs.delete()
                return Response({'status': 'ok', 'message': 'unfollowed'}, status=status.HTTP_200_OK)
            else:
                Follow.objects.create(follower_user=follower, followed_user=target)
                return Response({'status': 'ok', 'message': 'followed'}, status=status.HTTP_200_OK)
        
        if artist_id:
            target = get_object_or_404(Artist, id=artist_id)
            follow_qs = Follow.objects.filter(follower_user=follower, followed_artist=target)
            if follow_qs.exists():
                follow_qs.delete()
                return Response({'status': 'ok', 'message': 'unfollowed'}, status=status.HTTP_200_OK)
            else:
                Follow.objects.create(follower_user=follower, followed_artist=target)
                return Response({'status': 'ok', 'message': 'followed'}, status=status.HTTP_200_OK)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class LikedSongsView(APIView):
    """List of songs liked by the user, paginated and sorted by date"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لیست آهنگ‌های پسندیده شده",
        description="دریافت لیست آهنگ‌هایی که کاربر لایک کرده است (به ترتیب زمان).",
        responses={200: LikedSongSerializer(many=True)}
    )
    def get(self, request):
        user = request.user
        # Allow callers (e.g. HomeSummaryView) to force summary serializer selection
        summary_mode = getattr(self, 'force_summary', False) or (request.query_params.get('summary') == 'true')
        # pagination defaults
        try:
            page = int(request.query_params.get('page', 1))
        except Exception:
            page = 1
        try:
            page_size = int(request.query_params.get('page_size', 10))
        except Exception:
            page_size = 10
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 10
        start = (page - 1) * page_size
        end = start + page_size
        # pagination support
        try:
            page = int(request.query_params.get('page', 1))
        except Exception:
            page = 1
        try:
            page_size = int(request.query_params.get('page_size', 10))
        except Exception:
            page_size = 10
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 10
        start = (page - 1) * page_size
        end = start + page_size
        qs = SongLike.objects.filter(user=user).order_by('-created_at')
        
        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(qs, request)
        serializer = LikedSongSerializer(result_page, many=True, context={'request': request})
        
        return paginator.get_paginated_response(serializer.data)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class LikedAlbumsView(APIView):
    """List of albums liked by the user, paginated and sorted by date"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لیست آلبوم‌های پسندیده شده",
        description="دریافت لیست آلبوم‌هایی که کاربر لایک کرده است (به ترتیب زمان).",
        responses={200: LikedAlbumSerializer(many=True)}
    )
    def get(self, request):
        user = request.user
        qs = AlbumLike.objects.filter(user=user).order_by('-created_at')
        
        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(qs, request)
        serializer = LikedAlbumSerializer(result_page, many=True, context={'request': request})
        
        return paginator.get_paginated_response(serializer.data)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class LikedPlaylistsView(APIView):
    """List of playlists liked by the user, paginated and sorted by date"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لیست پلی‌لیست‌های پسندیده شده",
        description="دریافت لیست پلی‌لیست‌هایی که کاربر لایک کرده است (به ترتیب زمان).",
        responses={200: LikedPlaylistSerializer(many=True)}
    )
    def get(self, request):
        user = request.user
        qs = PlaylistLike.objects.filter(user=user).order_by('-created_at')
        
        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(qs, request)
        serializer = LikedPlaylistSerializer(result_page, many=True, context={'request': request})
        
        return paginator.get_paginated_response(serializer.data)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class MyArtistsView(APIView):
    """List of artists followed by the user, paginated and sorted by date"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لیست هنرمندان دنبال شده",
        description="دریافت لیست هنرمندانی که کاربر دنبال می‌کند.",
        responses={200: ArtistSerializer(many=True)}
    )
    def get(self, request):
        user = request.user
        # Filter follows where this user is the follower and the target is an artist
        qs = Follow.objects.filter(follower_user=user, followed_artist__isnull=False).order_by('-created_at')
        
        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(qs, request)
        
        # We want to return the artist data, but we have Follow objects.
        # We can use a SerializerMethodField or just map them.
        results = []
        for follow in result_page:
            artist_data = ArtistSerializer(follow.followed_artist, context={'request': request}).data
            artist_data['followed_at'] = follow.created_at
            results.append(artist_data)

        return paginator.get_paginated_response(results)


@extend_schema(tags=['Library Page Endpoints اندپوینت های صفحه کتابخانه'])
class MyLibraryView(APIView):
    """
    User's library history.
    Supports 'mix' mode (all types) or specific 'type' param.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="تاریخچه کتابخانه کاربر",
        description="دریافت تاریخچه بازدیدها و فعالیت‌های کاربر در کتابخانه (آهنگ، آلبوم، پلی‌لیست، هنرمند).",
        parameters=[
            OpenApiParameter("type", OpenApiTypes.STR, description="نوع محتوا (song, album, playlist, artist)"),
            OpenApiParameter("page", OpenApiTypes.INT, description="شماره صفحه"),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="تعداد در هر صفحه")
        ],
        responses={
            200: inline_serializer(
                name='MyLibraryResponse',
                fields={
                    'items': inline_serializer(
                        name='LibraryItem',
                        fields={
                            'id': serializers.IntegerField(),
                            'type': serializers.CharField(),
                            'viewed_at': serializers.DateTimeField(),
                            'item': serializers.DictField(),
                        },
                        many=True
                    ),
                    'total': serializers.IntegerField(),
                    'page': serializers.IntegerField(),
                    'has_next': serializers.BooleanField(),
                }
            )
        }
    )
    def get(self, request):
        content_type = request.query_params.get('type')
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        offset = (page - 1) * page_size

        qs = UserHistory.objects.filter(user=request.user).order_by('-updated_at')

        if content_type:
            if content_type not in [UserHistory.TYPE_SONG, UserHistory.TYPE_ALBUM, UserHistory.TYPE_PLAYLIST, UserHistory.TYPE_ARTIST]:
                return Response({"detail": "Invalid type."}, status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(content_type=content_type)

        total = qs.count()
        items = qs[offset:offset + page_size]
        
        results = []
        for entry in items:
            data = {
                'id': entry.id,
                'type': entry.content_type,
                'viewed_at': entry.updated_at,
            }
            
            if entry.content_type == UserHistory.TYPE_SONG and entry.song:
                data['item'] = SongStreamSerializer(entry.song, context={'request': request}).data
            elif entry.content_type == UserHistory.TYPE_ALBUM and entry.album:
                data['item'] = AlbumSerializer(entry.album, context={'request': request}).data
            elif entry.content_type == UserHistory.TYPE_PLAYLIST and entry.playlist:
                data['item'] = PlaylistSerializer(entry.playlist, context={'request': request}).data
            elif entry.content_type == UserHistory.TYPE_ARTIST and entry.artist:
                data['item'] = ArtistSerializer(entry.artist, context={'request': request}).data
            else:
                continue
                
            results.append(data)

        return Response({
            'items': results,
            'total': total,
            'page': page,
            'has_next': total > offset + page_size
        })


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class R2UploadView(APIView):
    """Upload a file to an S3-compatible R2 bucket and return a CDN URL."""
    permission_classes = [AllowAny]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="آپلود فایل به R2",
        description="آپلود مستقیم فایل به فضای ابری R2 و دریافت لینک CDN.",
        request=UploadSerializer,
        responses={
            201: inline_serializer(
                name='R2UploadResponse',
                fields={
                    'key': serializers.CharField(),
                    'url': serializers.CharField(),
                }
            )
        }
    )
    def post(self, request, *args, **kwargs):
        serializer = UploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        f = serializer.validated_data['file']
        folder = serializer.validated_data.get('folder', '').strip().strip('/')
        custom_filename = serializer.validated_data.get('filename')
        
        # get original filename and extension from uploaded file
        original_filename = getattr(f, 'name', None) or 'upload'
        
        if custom_filename:
            # if user provided custom filename, preserve extension from original file
            import os
            _, original_ext = os.path.splitext(original_filename)
            # check if custom filename already has an extension
            _, custom_ext = os.path.splitext(custom_filename)
            if custom_ext:
                # use custom filename as-is (user provided extension)
                filename = custom_filename
            else:
                # append original extension to custom filename
                filename = f"{custom_filename}{original_ext}"
        else:
            # no custom filename, use original
            filename = original_filename

        # build key: folder/filename (no unique prefix, use exact filename)
        key = f"{folder + '/' if folder else ''}{filename}"

        # Build boto3 client kwargs and avoid sending an empty session token
        client_kwargs = {
            'service_name': 's3',
            'endpoint_url': getattr(settings, 'R2_ENDPOINT_URL', None),
            'aws_access_key_id': getattr(settings, 'R2_ACCESS_KEY_ID', None),
            'aws_secret_access_key': getattr(settings, 'R2_SECRET_ACCESS_KEY', None),
            # Cloudflare R2 requires signature v4
            'config': Config(signature_version='s3v4'),
        }
        session_token = getattr(settings, 'R2_SESSION_TOKEN', None)
        if session_token:
            client_kwargs['aws_session_token'] = session_token

        # remove None values to avoid boto3 sending invalid headers
        client_kwargs = {k: v for k, v in client_kwargs.items() if v is not None}

        s3 = boto3.client(**client_kwargs)

        # Detect content type from file extension to preserve format
        import mimetypes
        content_type, _ = mimetypes.guess_type(filename)
        if not content_type:
            content_type = 'application/octet-stream'

        try:
            # upload_fileobj streams the file directly with content type
            s3.upload_fileobj(
                f, 
                getattr(settings, 'R2_BUCKET_NAME'), 
                key,
                ExtraArgs={'ContentType': content_type}
            )
        except ClientError as e:
            # Return a clearer error and include AWS error code/message
            err = e.response.get('Error', {})
            code = err.get('Code')
            msg = err.get('Message') or str(e)
            detail = f"{code}: {msg}" if code else str(e)
            # common cause: invalid/extra session token (X-Amz-Security-Token)
            if 'Security-Token' in detail or 'X-Amz-Security-Token' in detail:
                detail += ' — check R2_SESSION_TOKEN: remove it unless you are using temporary credentials.'
            return Response({'detail': detail}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception as e:
            return Response({'detail': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
        url = f"{cdn_base}/{key}"
        return Response({'key': key, 'url': url}, status=status.HTTP_201_CREATED)


# Helper functions moved to utils.py



@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class SongUploadView(APIView):
    """
    Upload song with audio file and metadata.
    Accepts mp3 and wav files, uploads to R2, and creates Song record.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    
    @extend_schema(
        summary="آپلود آهنگ جدید",
        description="آپلود فایل صوتی آهنگ به همراه متادیتا و تصویر کاور.",
        request=SongUploadSerializer,
        responses={201: SongSerializer}
    )
    def post(self, request, *args, **kwargs):
        print(f"DEBUG: SongUploadView.post started for user {request.user}")
        serializer = SongUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        
        try:
            # Get artist
            artist = Artist.objects.get(id=data['artist_id'])
            
            # Build filename: "Artist - Title (feat. X)" or "Artist - Title"
            title = data['title']
            featured = data.get('featured_artists', [])
            artist_name = artist.artistic_name or artist.name
            if featured:
                filename_base = f"{artist_name} - {title} (feat. {', '.join(featured)})"
            else:
                filename_base = f"{artist_name} - {title}"
            
            safe_filename_base = make_safe_filename(filename_base)
            
            # Upload audio file
            audio_file = data['audio_file']
            audio_filename = f"{safe_filename_base}.{audio_file.name.split('.')[-1]}"
            audio_url, original_format = upload_file_to_r2(
                audio_file,
                folder='songs',
                custom_filename=audio_filename
            )
            
            # Get audio info
            duration, bitrate, original_format = get_audio_info(audio_file)
            if not original_format:
                original_format = audio_file.name.split('.')[-1].lower()
            
            # Convert to 128kbps and upload
            converted_audio_url = None
            print(f"DEBUG: SongUploadView: format={original_format}, bitrate={bitrate}")
            if original_format != 'mp3' or bitrate is None or bitrate > 128:
                print(f"DEBUG: SongUploadView: Starting conversion...")
                try:
                    # Reset file pointer before conversion
                    if hasattr(audio_file, 'seek'):
                        audio_file.seek(0)
                    
                    converted_file = convert_to_128kbps(audio_file)
                    converted_filename = f"{safe_filename_base}_128.mp3"
                    print(f"DEBUG: SongUploadView: Uploading converted file...")
                    converted_audio_url, _ = upload_file_to_r2(
                        converted_file,
                        folder='songs/128',
                        custom_filename=converted_filename
                    )
                    print(f"DEBUG: SongUploadView: Converted URL: {converted_audio_url}")
                except Exception as e:
                    # Log error but don't fail the whole upload
                    print(f"DEBUG: SongUploadView: Conversion failed: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Upload cover image if provided
            cover_url = ""
            if data.get('cover_image'):
                cover_file = data['cover_image']
                cover_filename = f"{safe_filename_base}_cover.{cover_file.name.split('.')[-1]}"
                cover_url, _ = upload_file_to_r2(
                    cover_file,
                    folder='covers',
                    custom_filename=cover_filename
                )
            
            # Create song record
            song_data = {
                'title': title,
                'artist': artist,
                'featured_artists': featured,
                'audio_file': audio_url,
                'converted_audio_url': converted_audio_url,
                'cover_image': cover_url,
                'original_format': original_format,
                'duration_seconds': duration,
                'uploader': request.user,
                'is_single': data.get('is_single', False),
                'release_date': data.get('release_date'),
                'language': data.get('language', 'fa'),
                'description': data.get('description', ''),
                'lyrics': data.get('lyrics', ''),
                'tempo': data.get('tempo'),
                'energy': data.get('energy'),
                'danceability': data.get('danceability'),
                'valence': data.get('valence'),
                'acousticness': data.get('acousticness'),
                'instrumentalness': data.get('instrumentalness'),
                'speechiness': data.get('speechiness'),
                'live_performed': data.get('live_performed', False),
                'label': data.get('label', ''),
                'producers': data.get('producers', []),
                'composers': data.get('composers', []),
                'lyricists': data.get('lyricists', []),
                'credits': data.get('credits', ''),
            }
            print(f"DEBUG: SongUploadView: Final song_data: {song_data}")
            
            # Add album if provided
            if data.get('album_id'):
                song_data['album'] = Album.objects.get(id=data['album_id'])
            
            song = Song.objects.create(**song_data)
            
            # Add many-to-many relationships
            if data.get('genre_ids'):
                song.genres.set(Genre.objects.filter(id__in=data['genre_ids']))
            if data.get('sub_genre_ids'):
                song.sub_genres.set(SubGenre.objects.filter(id__in=data['sub_genre_ids']))
            if data.get('mood_ids'):
                song.moods.set(Mood.objects.filter(id__in=data['mood_ids']))
            if data.get('tag_ids'):
                song.tags.set(Tag.objects.filter(id__in=data['tag_ids']))
            
            return Response(
                SongSerializer(song).data,
                status=status.HTTP_201_CREATED
            )
            
        except Artist.DoesNotExist:
            return Response(
                {'error': 'Artist not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Album.DoesNotExist:
            return Response(
                {'error': 'Album not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class ArtistListView(APIView):
    """List and Create Artists"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    @extend_schema(
        summary="لیست هنرمندان",
        description="دریافت لیست تمامی هنرمندان ثبت شده در سامانه.",
        responses={200: ArtistSerializer(many=True)}
    )
    def get(self, request):
        artists = Artist.objects.all()
        serializer = ArtistSerializer(artists, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ایجاد هنرمند جدید",
        description="ثبت یک هنرمند جدید در سامانه (نیازمند احراز هویت).",
        request=ArtistSerializer,
        responses={201: ArtistSerializer}
    )
    def post(self, request):
        serializer = ArtistSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class PlaylistDetailView(APIView):
    """Retrieve, Update, and Delete Playlist (Admin/System/Audience)"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return Playlist.objects.get(pk=pk)
        except Playlist.DoesNotExist:
            return None

    @extend_schema(
        summary="جزئیات پلی‌لیست",
        description="دریافت اطلاعات کامل یک پلی‌لیست به همراه لیست آهنگ‌ها.",
        responses={200: PlaylistSerializer}
    )
    def get(self, request, pk):
        playlist = self.get_object(pk)
        if not playlist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        
        # Record history
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(
                user=request.user,
                content_type=UserHistory.TYPE_PLAYLIST,
                playlist=playlist,
                defaults={'updated_at': timezone.now()}
            )
            
        serializer = PlaylistSerializer(playlist, context={'request': request})
        return Response(serializer.data)

   

   
@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class PlaylistLikeView(APIView):
    """Like or unlike a playlist (Admin/System/Audience)"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لایک کردن پلی‌لیست",
        description="لایک کردن یا لغو لایک یک پلی‌لیست.",
        responses={
            200: inline_serializer(
                name='PlaylistLikeResponse',
                fields={
                    'liked': serializers.BooleanField(),
                    'likes_count': serializers.IntegerField(),
                }
            )
        }
    )
    def post(self, request, pk):
        try:
            playlist = Playlist.objects.get(pk=pk)
        except Playlist.DoesNotExist:
            return Response({"detail": "Playlist not found."}, status=status.HTTP_404_NOT_FOUND)
        
        user = request.user
        like_qs = PlaylistLike.objects.filter(user=user, playlist=playlist)
        if like_qs.exists():
            like_qs.delete()
            liked = False
        else:
            PlaylistLike.objects.create(user=user, playlist=playlist)
            liked = True
            
        return Response({
            "liked": liked,
            "likes_count": playlist.liked_by.count()
        })


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class ArtistDetailView(APIView):
    """Retrieve, Update, and Delete Artist"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return Artist.objects.get(pk=pk)
        except Artist.DoesNotExist:
            return None

    @extend_schema(
        summary="جزئیات هنرمند",
        description="دریافت اطلاعات کامل هنرمند به همراه برترین آهنگ‌ها، آلبوم‌ها و آهنگ‌های جدید.",
        parameters=[
            OpenApiParameter("type", OpenApiTypes.STR, description="نوع لیست درخواستی (top_songs, albums, latest_songs)"),
            OpenApiParameter("page", OpenApiTypes.INT, description="شماره صفحه"),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="تعداد در هر صفحه")
        ],
        responses={
            200: inline_serializer(
                name='ArtistDetailResponse',
                fields={
                    'artist': ArtistSerializer(),
                    'top_songs': inline_serializer(
                        name='ArtistTopSongs',
                        fields={
                            'items': SongStreamSerializer(many=True),
                            'total': serializers.IntegerField(),
                            'next_page_link': serializers.CharField(allow_null=True),
                        }
                    ),
                    'albums': inline_serializer(
                        name='ArtistAlbums',
                        fields={
                            'items': AlbumSerializer(many=True),
                            'total': serializers.IntegerField(),
                            'next_page_link': serializers.CharField(allow_null=True),
                        }
                    ),
                    'latest_songs': inline_serializer(
                        name='ArtistLatestSongs',
                        fields={
                            'items': SongStreamSerializer(many=True),
                            'total': serializers.IntegerField(),
                            'next_page_link': serializers.CharField(allow_null=True),
                        }
                    ),
                    'discovered_on': serializers.ListField(child=serializers.DictField()),
                    'similar_artists': PopularArtistSerializer(many=True),
                }
            )
        }
    )
    def get(self, request, pk):
        artist = self.get_object(pk)
        if not artist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        
        # Record history
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(
                user=request.user,
                content_type=UserHistory.TYPE_ARTIST,
                artist=artist,
                defaults={'updated_at': timezone.now()}
            )
        
        # Pagination params
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 10))
        offset = (page - 1) * page_size
        
        # Check if user wants a specific list (paginated)
        list_type = request.query_params.get('type')
        
        if list_type == 'top_songs':
            qs = Song.objects.filter(artist=artist, status=Song.STATUS_PUBLISHED).annotate(
                total_plays=Coalesce(F('plays'), 0) + Count('play_counts')
            ).order_by('-total_plays')
            items = qs[offset:offset + page_size]
            data = SongStreamSerializer(items, many=True, context={'request': request}).data
            return Response({
                'items': data,
                'total': qs.count(),
                'page': page,
                'has_next': qs.count() > offset + page_size
            })
            
        if list_type == 'albums':
            qs = Album.objects.filter(artist=artist).exclude(title__iexact='single').order_by('-release_date')
            items = qs[offset:offset + page_size]
            data = AlbumSerializer(items, many=True, context={'request': request}).data
            return Response({
                'items': data,
                'total': qs.count(),
                'page': page,
                'has_next': qs.count() > offset + page_size
            })
            
        if list_type == 'latest_songs':
            qs = Song.objects.filter(artist=artist, status=Song.STATUS_PUBLISHED).order_by('-release_date', '-created_at')
            items = qs[offset:offset + page_size]
            data = SongStreamSerializer(items, many=True, context={'request': request}).data
            return Response({
                'items': data,
                'total': qs.count(),
                'page': page,
                'has_next': qs.count() > offset + page_size
            })

        # Default: Return full detail view
        # Basic artist data
        artist_data = ArtistSerializer(artist, context={'request': request}).data
        
        # 1. Top Songs (preview)
        top_songs_qs = Song.objects.filter(artist=artist, status=Song.STATUS_PUBLISHED).annotate(
            total_plays=Coalesce(F('plays'), 0) + Count('play_counts')
        ).order_by('-total_plays')
        top_songs_data = SongStreamSerializer(top_songs_qs[:5], many=True, context={'request': request}).data
        
        # 2. Albums (preview)
        albums_qs = Album.objects.filter(artist=artist).exclude(title__iexact='single').order_by('-release_date')
        albums_data = AlbumSerializer(albums_qs[:5], many=True, context={'request': request}).data
        
        # 3. Latest Songs (preview)
        latest_songs_qs = Song.objects.filter(artist=artist, status=Song.STATUS_PUBLISHED).order_by('-release_date', '-created_at')
        latest_songs_data = SongStreamSerializer(latest_songs_qs[:5], many=True, context={'request': request}).data
        
        # 4. Discovered On
        discovered_on = []
        
        # Admin playlists
        admin_playlists = Playlist.objects.filter(songs__artist=artist, created_by=Playlist.CREATED_BY_ADMIN).distinct()
        # System/Audience playlists with likes
        other_playlists = Playlist.objects.filter(
            songs__artist=artist,
            created_by__in=[Playlist.CREATED_BY_SYSTEM, Playlist.CREATED_BY_AUDIENCE]
        ).annotate(likes_count=Count('liked_by')).filter(likes_count__gt=0).distinct()
        # Public UserPlaylists with likes
        user_playlists = UserPlaylist.objects.filter(songs__artist=artist, public=True).annotate(likes_count=Count('liked_by')).filter(likes_count__gt=0).distinct()
        
        # Credited songs
        credited_songs = Song.objects.filter(
            Q(featured_artists__icontains=artist.name) |
            Q(producers__icontains=artist.name) |
            Q(composers__icontains=artist.name) |
            Q(lyricists__icontains=artist.name)
        ).exclude(artist=artist).filter(status=Song.STATUS_PUBLISHED).distinct()
        
        for p in admin_playlists:
            discovered_on.append({'type': 'playlist', 'id': p.id, 'title': p.title, 'image': p.cover_image, 'source': 'admin'})
        for p in other_playlists:
            discovered_on.append({'type': 'playlist', 'id': p.id, 'title': p.title, 'image': p.cover_image, 'source': p.created_by})
        for p in user_playlists:
            discovered_on.append({'type': 'user_playlist', 'id': p.id, 'title': p.title, 'image': None, 'source': 'user'})
        for s in credited_songs:
            discovered_on.append({'type': 'song', 'id': s.id, 'title': s.title, 'image': s.cover_image, 'artist': s.artist.name if s.artist else None})

        base_url = request.build_absolute_uri(request.path)
        # 5. Similar artists: score candidates by genre/mood overlap, shared monthly listeners and shared followers
        # Gather artist's genre/mood ids from their songs
        genre_ids = list(Song.objects.filter(artist=artist).values_list('genres__id', flat=True))
        mood_ids = list(Song.objects.filter(artist=artist).values_list('moods__id', flat=True))
        genre_ids = [g for g in set(genre_ids) if g]
        mood_ids = [m for m in set(mood_ids) if m]

        # Followers (user ids) who follow this artist
        follower_user_ids = list(Follow.objects.filter(followed_artist=artist, follower_user__isnull=False).values_list('follower_user_id', flat=True))

        # Monthly listeners (user ids) for this artist
        monthly_user_ids = list(ArtistMonthlyListener.objects.filter(artist=artist).values_list('user_id', flat=True))

        # Build candidate artists and annotate overlaps
        candidates = Artist.objects.exclude(id=artist.id).annotate(
            genre_overlap=Count('songs__genres', filter=Q(songs__genres__in=genre_ids), distinct=True),
            mood_overlap=Count('songs__moods', filter=Q(songs__moods__in=mood_ids), distinct=True),
            shared_listeners=Count('monthly_listener_records', filter=Q(monthly_listener_records__user__in=monthly_user_ids), distinct=True),
            shared_followers=Count('follower_artist_relations', filter=Q(follower_artist_relations__follower_user__in=follower_user_ids), distinct=True),
        )

        from django.db.models import ExpressionWrapper, FloatField
        score_expr = ExpressionWrapper(
            F('genre_overlap') * 3 + F('mood_overlap') * 2 + F('shared_listeners') * 2 + F('shared_followers') * 1,
            output_field=FloatField()
        )

        candidates = candidates.annotate(similarity_score=score_expr).filter(similarity_score__gt=0).order_by('-similarity_score')[:6]
        similar_artists_data = PopularArtistSerializer(candidates, many=True, context={'request': request}).data

        return Response({
            'artist': artist_data,
            'top_songs': {
                'items': top_songs_data,
                'total': top_songs_qs.count(),
                'next_page_link': f"{base_url}?type=top_songs&page=2" if top_songs_qs.count() > 5 else None
            },
            'albums': {
                'items': albums_data,
                'total': albums_qs.count(),
                'next_page_link': f"{base_url}?type=albums&page=2" if albums_qs.count() > 5 else None
            },
            'latest_songs': {
                'items': latest_songs_data,
                'total': latest_songs_qs.count(),
                'next_page_link': f"{base_url}?type=latest_songs&page=2" if latest_songs_qs.count() > 5 else None
            },
            'discovered_on': discovered_on[:10]
            ,
            'similar_artists': similar_artists_data
        })

   

@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class AlbumListView(APIView):
    """List and Create Albums"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    @extend_schema(
        summary="لیست آلبوم‌ها",
        description="دریافت لیست تمامی آلبوم‌های ثبت شده در سامانه.",
        responses={200: AlbumSerializer(many=True)}
    )
    def get(self, request):
        albums = Album.objects.all()
        serializer = AlbumSerializer(albums, many=True, context={'request': request})
        return Response(serializer.data)

    


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class AlbumDetailView(APIView):
    """Retrieve, Update, and Delete Album"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return Album.objects.get(pk=pk)
        except Album.DoesNotExist:
            return None

    @extend_schema(
        summary="جزئیات آلبوم",
        description="دریافت اطلاعات کامل یک آلبوم به همراه لیست آهنگ‌ها.",
        responses={200: AlbumSerializer}
    )
    def get(self, request, pk):
        album = self.get_object(pk)
        if not album:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        
        # Record history
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(
                user=request.user,
                content_type=UserHistory.TYPE_ALBUM,
                album=album,
                defaults={'updated_at': timezone.now()}
            )
            
        serializer = AlbumSerializer(album, context={'request': request})
        return Response(serializer.data)

   

@extend_schema(tags=['Classification اندپوینت های دسته‌بندی'])
class GenreListView(APIView):
    """List and Create Genres"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [permissions.IsAdminUser()]

    @extend_schema(
        summary="لیست سبک‌ها (ژانرها)",
        description="دریافت لیست تمامی سبک‌های موسیقی موجود در سامانه.",
        responses={200: GenreSerializer(many=True)}
    )
    def get(self, request):
        genres = Genre.objects.all()
        serializer = GenreSerializer(genres, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ایجاد سبک جدید (Admin Only)",
        description="ثبت یک سبک موسیقی جدید در سامانه.",
        request=GenreSerializer,
        responses={201: GenreSerializer}
    )
    def post(self, request):
        serializer = GenreSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Classification اندپوینت های دسته‌بندی'])
class GenreDetailView(APIView):
    """Retrieve, Update, and Delete Genre"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [permissions.IsAdminUser()]

    def get_object(self, pk):
        try:
            return Genre.objects.get(pk=pk)
        except Genre.DoesNotExist:
            return None

    @extend_schema(
        summary="جزئیات سبک",
        description="دریافت اطلاعات کامل یک سبک موسیقی.",
        responses={200: GenreSerializer}
    )
    def get(self, request, pk):
        genre = self.get_object(pk)
        if not genre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = GenreSerializer(genre, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش سبک (کامل) (Admin Only)",
        description="به‌روزرسانی تمامی اطلاعات یک سبک موسیقی.",
        request=GenreSerializer,
        responses={200: GenreSerializer}
    )
    def put(self, request, pk):
        genre = self.get_object(pk)
        if not genre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = GenreSerializer(genre, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="ویرایش سبک (جزئی) (Admin Only)",
        description="به‌روزرسانی برخی از اطلاعات یک سبک موسیقی.",
        request=GenreSerializer,
        responses={200: GenreSerializer}
    )
    def patch(self, request, pk):
        genre = self.get_object(pk)
        if not genre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = GenreSerializer(genre, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف سبک (Admin Only)",
        description="حذف یک سبک موسیقی از سامانه.",
        responses={204: OpenApiTypes.NONE}
    )
    def delete(self, request, pk):
        genre = self.get_object(pk)
        if not genre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        genre.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Classification اندپوینت های دسته‌بندی'])
class MoodListView(APIView):
    """List and Create Moods"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [permissions.IsAdminUser()]

    @extend_schema(
        summary="لیست حال و هواها (Moods)",
        description="دریافت لیست تمامی حال و هواهای موسیقی موجود در سامانه.",
        responses={200: MoodSerializer(many=True)}
    )
    def get(self, request):
        moods = Mood.objects.all()
        serializer = MoodSerializer(moods, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ایجاد حال و هوای جدید (Admin Only)",
        description="ثبت یک حال و هوای موسیقی جدید در سامانه.",
        request=MoodSerializer,
        responses={201: MoodSerializer}
    )
    def post(self, request):
        serializer = MoodSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Classification اندپوینت های دسته‌بندی'])
class MoodDetailView(APIView):
    """Retrieve, Update, and Delete Mood"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [permissions.IsAdminUser()]

    def get_object(self, pk):
        try:
            return Mood.objects.get(pk=pk)
        except Mood.DoesNotExist:
            return None

    @extend_schema(
        summary="جزئیات حال و هوا",
        description="دریافت اطلاعات کامل یک حال و هوای موسیقی.",
        responses={200: MoodSerializer}
    )
    def get(self, request, pk):
        mood = self.get_object(pk)
        if not mood:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = MoodSerializer(mood, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش حال و هوا (کامل) (Admin Only)",
        description="به‌روزرسانی تمامی اطلاعات یک حال و هوای موسیقی.",
        request=MoodSerializer,
        responses={200: MoodSerializer}
    )
    def put(self, request, pk):
        mood = self.get_object(pk)
        if not mood:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = MoodSerializer(mood, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="ویرایش حال و هوا (جزئی) (Admin Only)",
        description="به‌روزرسانی برخی از اطلاعات یک حال و هوای موسیقی.",
        request=MoodSerializer,
        responses={200: MoodSerializer}
    )
    def patch(self, request, pk):
        mood = self.get_object(pk)
        if not mood:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = MoodSerializer(mood, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف حال و هوا (Admin Only)",
        description="حذف یک حال و هوای موسیقی از سامانه.",
        responses={204: OpenApiTypes.NONE}
    )
    def delete(self, request, pk):
        mood = self.get_object(pk)
        if not mood:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        mood.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Classification اندپوینت های دسته‌بندی'])
class TagListView(APIView):
    """List and Create Tags"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [permissions.IsAdminUser()]

    @extend_schema(
        summary="لیست تگ‌ها",
        description="دریافت لیست تمامی تگ‌های موجود در سامانه.",
        responses={200: TagSerializer(many=True)}
    )
    def get(self, request):
        tags = Tag.objects.all()
        serializer = TagSerializer(tags, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ایجاد تگ جدید (Admin Only)",
        description="ثبت یک تگ جدید در سامانه.",
        request=TagSerializer,
        responses={201: TagSerializer}
    )
    def post(self, request):
        serializer = TagSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Classification اندپوینت های دسته‌بندی'])
class TagDetailView(APIView):
    """Retrieve, Update, and Delete Tag"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [permissions.IsAdminUser()]

    def get_object(self, pk):
        try:
            return Tag.objects.get(pk=pk)
        except Tag.DoesNotExist:
            return None

    @extend_schema(
        summary="جزئیات تگ",
        description="دریافت اطلاعات کامل یک تگ.",
        responses={200: TagSerializer}
    )
    def get(self, request, pk):
        tag = self.get_object(pk)
        if not tag:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = TagSerializer(tag, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش تگ (کامل) (Admin Only)",
        description="به‌روزرسانی تمامی اطلاعات یک تگ.",
        request=TagSerializer,
        responses={200: TagSerializer}
    )
    def put(self, request, pk):
        tag = self.get_object(pk)
        if not tag:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = TagSerializer(tag, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="ویرایش تگ (جزئی) (Admin Only)",
        description="به‌روزرسانی برخی از اطلاعات یک تگ.",
        request=TagSerializer,
        responses={200: TagSerializer}
    )
    def patch(self, request, pk):
        tag = self.get_object(pk)
        if not tag:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = TagSerializer(tag, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف تگ (Admin Only)",
        description="حذف یک تگ از سامانه.",
        responses={204: OpenApiTypes.NONE}
    )
    def delete(self, request, pk):
        tag = self.get_object(pk)
        if not tag:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        tag.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Classification اندپوینت های دسته‌بندی'])
class SubGenreListView(APIView):
    """List and Create SubGenres"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [permissions.IsAdminUser()]

    @extend_schema(
        summary="لیست زیرسبک‌ها",
        description="دریافت لیست تمامی زیرسبک‌های موسیقی موجود در سامانه.",
        responses={200: SubGenreSerializer(many=True)}
    )
    def get(self, request):
        subgenres = SubGenre.objects.all()
        serializer = SubGenreSerializer(subgenres, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ایجاد زیرسبک جدید (Admin Only)",
        description="ثبت یک زیرسبک موسیقی جدید در سامانه.",
        request=SubGenreSerializer,
        responses={201: SubGenreSerializer}
    )
    def post(self, request):
        serializer = SubGenreSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Classification اندپوینت های دسته‌بندی'])
class SubGenreDetailView(APIView):
    """Retrieve, Update, and Delete SubGenre"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [permissions.IsAdminUser()]

    def get_object(self, pk):
        try:
            return SubGenre.objects.get(pk=pk)
        except SubGenre.DoesNotExist:
            return None

    @extend_schema(
        summary="جزئیات زیرسبک",
        description="دریافت اطلاعات کامل یک زیرسبک موسیقی.",
        responses={200: SubGenreSerializer}
    )
    def get(self, request, pk):
        subgenre = self.get_object(pk)
        if not subgenre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SubGenreSerializer(subgenre, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش زیرسبک (کامل) (Admin Only)",
        description="به‌روزرسانی تمامی اطلاعات یک زیرسبک موسیقی.",
        request=SubGenreSerializer,
        responses={200: SubGenreSerializer}
    )
    def put(self, request, pk):
        subgenre = self.get_object(pk)
        if not subgenre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SubGenreSerializer(subgenre, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="ویرایش زیرسبک (جزئی) (Admin Only)",
        description="به‌روزرسانی برخی از اطلاعات یک زیرسبک موسیقی.",
        request=SubGenreSerializer,
        responses={200: SubGenreSerializer}
    )
    def patch(self, request, pk):
        subgenre = self.get_object(pk)
        if not subgenre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SubGenreSerializer(subgenre, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف زیرسبک (Admin Only)",
        description="حذف یک زیرسبک موسیقی از سامانه.",
        responses={204: OpenApiTypes.NONE}
    )
    def delete(self, request, pk):
        subgenre = self.get_object(pk)
        if not subgenre:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        subgenre.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class SongListView(generics.ListCreateAPIView):
    """View for listing and creating songs"""
    serializer_class = SongSerializer
    permission_classes = [IsAuthenticated]
    
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return super().get_permissions()
    
    @extend_schema(
        summary="لیست آهنگ‌ها",
        description="دریافت لیست تمامی آهنگ‌های منتشر شده در سامانه.",
        responses={200: SongSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        summary="ایجاد آهنگ جدید",
        description="ثبت یک آهنگ جدید در سامانه.",
        request=SongSerializer,
        responses={201: SongSerializer}
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def get_queryset(self):
        """Filter songs by status for non-staff users"""
        queryset = Song.objects.all()
        
        # Non-authenticated or non-staff users only see published songs
        if not self.request.user.is_authenticated or not self.request.user.is_staff:
            queryset = queryset.filter(status=Song.STATUS_PUBLISHED)
        
        return queryset


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class SongDetailView(APIView):
    """View for retrieving, updating and deleting a song"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            song = Song.objects.get(pk=pk)
            # Non-authenticated or non-staff users only see published songs
            if not self.request.user.is_authenticated or not self.request.user.is_staff:
                if song.status != Song.STATUS_PUBLISHED:
                    return None
            return song
        except Song.DoesNotExist:
            return None

    @extend_schema(
        summary="جزئیات آهنگ",
        description="دریافت اطلاعات کامل یک آهنگ به همراه آمار بازدید (برای هنرمند آهنگ).",
        parameters=[
            OpenApiParameter("days", OpenApiTypes.INT, description="تعداد روزها برای آمار (پیش‌فرض ۳۰)")
        ],
        responses={200: SongSerializer}
    )
    def get(self, request, pk):
        song = self.get_object(pk)
        if not song:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        
        # Record history
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(
                user=request.user,
                content_type=UserHistory.TYPE_SONG,
                song=song,
                defaults={'updated_at': timezone.now()}
            )
            
        serializer = SongSerializer(song, context={'request': request})
        data = serializer.data

        # If the user is the artist of this song, add detailed analytics
        is_artist = False
        if request.user.is_authenticated:
            try:
                artist_profile = request.user.artist_profile
                if song.artist == artist_profile:
                    is_artist = True
            except Artist.DoesNotExist:
                pass

        if is_artist:
            try:
                days = int(request.query_params.get('days', 30))
            except (ValueError, TypeError):
                days = 30
            
            start_date = timezone.now() - timedelta(days=days)
            period_plays = song.play_counts.filter(created_at__gte=start_date)
            total_period_plays = period_plays.count()
            
            daily_plays = period_plays.annotate(date=TruncDate('created_at')) \
                .values('date').annotate(count=Count('id')).order_by('date')
            
            city_dist = period_plays.values('city').annotate(count=Count('id')).order_by('-count')
            city_data = []
            for item in city_dist:
                percentage = (item['count'] / total_period_plays * 100) if total_period_plays > 0 else 0
                city_data.append({
                    'city': item['city'],
                    'count': item['count'],
                    'percentage': round(percentage, 2)
                })
                
            country_dist = period_plays.values('country').annotate(count=Count('id')).order_by('-count')
            country_data = []
            for item in country_dist:
                percentage = (item['count'] / total_period_plays * 100) if total_period_plays > 0 else 0
                country_data.append({
                    'country': item['country'],
                    'count': item['count'],
                    'percentage': round(percentage, 2)
                })
                
            data['analytics'] = {
                'days': days,
                'total_period_plays': total_period_plays,
                'daily_plays': list(daily_plays),
                'city_distribution': city_data,
                'country_distribution': country_data
            }

        return Response(data)

   


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class SongLikeView(APIView):
    """Toggle like status for a song"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لایک کردن آهنگ",
        description="لایک کردن یا لغو لایک یک آهنگ.",
        responses={
            200: inline_serializer(
                name='SongLikeResponse',
                fields={
                    'liked': serializers.BooleanField(),
                    'likes_count': serializers.IntegerField(),
                }
            )
        }
    )
    def post(self, request, pk=None):
        try:
            song = Song.objects.get(pk=pk)
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)
            
        user = request.user
        like_qs = SongLike.objects.filter(user=user, song=song)
        if like_qs.exists():
            like_qs.delete()
            liked = False
        else:
            SongLike.objects.create(user=user, song=song)
            liked = True
            
        return Response({
            'liked': liked,
            'likes_count': SongLike.objects.filter(song=song).count()
        })


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class AlbumLikeView(APIView):
    """Toggle like status for an album"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لایک کردن آلبوم",
        description="لایک کردن یا لغو لایک یک آلبوم.",
        responses={
            200: inline_serializer(
                name='AlbumLikeResponse',
                fields={
                    'liked': serializers.BooleanField(),
                    'likes_count': serializers.IntegerField(),
                }
            )
        }
    )
    def post(self, request, pk=None):
        try:
            album = Album.objects.get(pk=pk)
        except Album.DoesNotExist:
            return Response({'error': 'Album not found'}, status=status.HTTP_404_NOT_FOUND)
            
        user = request.user
        like_qs = AlbumLike.objects.filter(user=user, album=album)
        if like_qs.exists():
            like_qs.delete()
            liked = False
        else:
            AlbumLike.objects.create(user=user, album=album)
            liked = True
            
        return Response({
            'liked': liked,
            'likes_count': AlbumLike.objects.filter(album=album).count()
        })


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class SongIncrementPlaysView(APIView):
    """Increment play count for a song"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="افزایش تعداد پخش آهنگ",
        description="افزایش تعداد دفعات پخش یک آهنگ (به صورت دستی).",
        responses={
            200: inline_serializer(
                name='SongIncrementPlaysResponse',
                fields={
                    'plays': serializers.IntegerField(),
                }
            )
        }
    )
    def post(self, request, pk=None):
        try:
            song = Song.objects.get(pk=pk)
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)
            
        song.plays += 1
        song.save(update_fields=['plays'])
        return Response({'plays': song.plays})


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class SongStreamListView(generics.ListAPIView):
    """
    List songs with wrapper stream URLs that require unwrapping.
    Returns songs with stream_url field that points to unwrap endpoint.
    """
    serializer_class = SongStreamSerializer
    permission_classes = [IsAuthenticated]
    
    @extend_schema(
        summary="لیست آهنگ‌ها برای پخش",
        description="دریافت لیست آهنگ‌ها به همراه توکن‌های پخش (Stream Tokens).",
        parameters=[
            OpenApiParameter("artist", OpenApiTypes.INT, description="فیلتر بر اساس هنرمند"),
            OpenApiParameter("album", OpenApiTypes.INT, description="فیلتر بر اساس آلبوم"),
            OpenApiParameter("genre", OpenApiTypes.INT, description="فیلتر بر اساس سبک"),
            OpenApiParameter("mood", OpenApiTypes.INT, description="فیلتر بر اساس حال و هوا")
        ],
        responses={200: SongStreamSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        """Filter songs by status for non-staff users"""
        queryset = Song.objects.all()
        
        # Non-staff users only see published songs
        if not self.request.user.is_staff:
            queryset = queryset.filter(status=Song.STATUS_PUBLISHED)
        
        # Filter by artist
        artist_id = self.request.query_params.get('artist')
        if artist_id:
            queryset = queryset.filter(artist_id=artist_id)
        
        # Filter by album
        album_id = self.request.query_params.get('album')
        if album_id:
            queryset = queryset.filter(album_id=album_id)
        
        # Filter by genre
        genre_id = self.request.query_params.get('genre')
        if genre_id:
            queryset = queryset.filter(genres__id=genre_id)
        
        # Filter by mood
        mood_id = self.request.query_params.get('mood')
        if mood_id:
            queryset = queryset.filter(moods__id=mood_id)
        
        return queryset.distinct()


# Helper functions moved to utils.py



@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class UnwrapStreamView(APIView):
    """
    Unwrap a stream URL token to get the actual signed URL.
    Tracks unwraps and injects ad URLs based on PlayConfiguration.
    """
    permission_classes = [IsAuthenticated]
    
    @extend_schema(
        summary="باز کردن توکن پخش (Unwrap)",
        description="تبدیل توکن پخش به لینک مستقیم و امضا شده فایل صوتی. ممکن است منجر به نمایش تبلیغ شود.",
        responses={
            200: inline_serializer(
                name='UnwrapResponse',
                fields={
                    'type': serializers.ChoiceField(choices=['stream', 'ad']),
                    'url': serializers.CharField(required=False),
                    'song_id': serializers.IntegerField(required=False),
                    'song_title': serializers.CharField(required=False),
                    'expires_in': serializers.IntegerField(required=False, allow_null=True),
                    'unwrap_count': serializers.IntegerField(),
                    'unique_otplay_id': serializers.CharField(required=False),
                    'ad': AudioAdSerializer(required=False),
                    'submit_id': serializers.CharField(required=False),
                    'message': serializers.CharField(required=False),
                    'pending': serializers.BooleanField(required=False),
                }
            )
        }
    )
    def get(self, request, token):
        try:
            # Get the stream access record
            stream_access = StreamAccess.objects.select_related('song', 'user').get(
                unwrap_token=token,
                user=request.user
            )
            
            # Check if already unwrapped
            if stream_access.unwrapped:
                return Response(
                    {'error': 'This stream token has already been used'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Check if user has any pending ads (required but not seen) from previous requests
            pending_ad = StreamAccess.objects.filter(user=request.user, ad_required=True, ad_seen=False).first()
            if pending_ad:
                return Response({
                    'type': 'ad',
                    'ad': AudioAdSerializer(pending_ad.ad_object).data,
                    'submit_id': pending_ad.ad_submit_id,
                    'message': 'You must finish watching the previous advertisement',
                    'pending': True
                })
            
            # Mark as unwrapped
            stream_access.unwrapped = True
            stream_access.unwrapped_at = timezone.now()
            stream_access.save(update_fields=['unwrapped', 'unwrapped_at'])
            
            # Count unwrapped streams for this user (last 24 hours for fairness)
            cutoff_time = timezone.now() - timedelta(hours=24)
            unwrapped_count = StreamAccess.objects.filter(
                user=request.user,
                unwrapped=True,
                unwrapped_at__gte=cutoff_time
            ).count()
            
            # Use ad frequency from configuration
            config = PlayConfiguration.objects.last()
            ad_freq = config.ad_frequency if config else 15
            
            if ad_freq > 0 and unwrapped_count % ad_freq == 0:
                # Pick a random active ad
                active_ads = AudioAd.objects.filter(is_active=True)
                if active_ads.exists():
                    ad = random.choice(active_ads)
                    submit_id = secrets.token_urlsafe(32)
                    
                    stream_access.ad_required = True
                    stream_access.ad_seen = False
                    stream_access.ad_submit_id = submit_id
                    stream_access.ad_object = ad
                    stream_access.save(update_fields=['ad_required', 'ad_seen', 'ad_submit_id', 'ad_object'])
                    
                    return Response({
                        'type': 'ad',
                        'ad': AudioAdSerializer(ad).data,
                        'submit_id': submit_id,
                        'message': 'Please listen to this brief advertisement',
                        'unwrap_count': unwrapped_count
                    })
            
            # No ad required, return stream response
            return self._get_stream_response(request, stream_access, unwrapped_count)
            
        except StreamAccess.DoesNotExist:
            return Response(
                {'error': 'Invalid or unauthorized stream token'},
                status=status.HTTP_404_NOT_FOUND
            )

    def _get_stream_response(self, request, stream_access, unwrapped_count):
        """Helper to generate the final stream response with quality selection"""
        song = stream_access.song
        
        # Quality selection: Default to low (128kbps) unless user is premium and chose high
        quality = request.user.settings.get('stream_quality', 'low')
        if quality == 'high' and song.audio_file:
            audio_url = song.audio_file
        elif song.converted_audio_url:
            audio_url = song.converted_audio_url
        else:
            audio_url = song.audio_file

        # Extract key for R2
        cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
        from urllib.parse import unquote, urlparse
        if audio_url.startswith(cdn_base):
            object_key = unquote(audio_url.replace(cdn_base + '/', ''))
        else:
            parsed = urlparse(audio_url)
            object_key = unquote(parsed.path.lstrip('/'))

        # Generate signed URL
        if audio_url and audio_url.startswith(cdn_base):
            signed_url = generate_signed_r2_url(object_key, expiration=3600)
            expires = 3600
        else:
            signed_url = audio_url
            expires = None

        # Record active playback for live listener count
        ActivePlayback.objects.filter(user=request.user).delete()
        duration = song.duration_seconds or 0
        expiration_time = timezone.now() + timedelta(seconds=duration)
        ActivePlayback.objects.create(
            user=request.user,
            song=song,
            expiration_time=expiration_time
        )

        return Response({
            'type': 'stream',
            'url': signed_url,
            'song_id': song.id,
            'song_title': song.display_title,
            'expires_in': expires,
            'unwrap_count': unwrapped_count,
            'unique_otplay_id': stream_access.unique_otplay_id
        })


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class StreamShortRedirectView(APIView):
    """
    Short URL redirect that generates signed URL on-the-fly.
    Much shorter URLs while maintaining security and ad injection.
    """
    permission_classes = [IsAuthenticated]
    
    @extend_schema(
        summary="باز کردن لینک کوتاه پخش",
        description="تبدیل لینک کوتاه پخش به لینک مستقیم و امضا شده فایل صوتی.",
        responses={
            200: inline_serializer(
                name='StreamShortResponse',
                fields={
                    'type': serializers.ChoiceField(choices=['stream', 'ad']),
                    'url': serializers.CharField(required=False),
                    'song_id': serializers.IntegerField(required=False),
                    'song_title': serializers.CharField(required=False),
                    'expires_in': serializers.IntegerField(required=False, allow_null=True),
                    'unwrap_count': serializers.IntegerField(),
                    'unique_otplay_id': serializers.CharField(required=False),
                    'ad': AudioAdSerializer(required=False),
                    'submit_id': serializers.CharField(required=False),
                    'message': serializers.CharField(required=False),
                    'pending': serializers.BooleanField(required=False),
                }
            )
        }
    )
    def get(self, request, token):
        try:
            # Get the stream access record
            stream_access = StreamAccess.objects.select_related('song', 'user').get(
                short_token=token,
                user=request.user
            )
            
            # Check if already unwrapped
            if stream_access.unwrapped:
                return Response(
                    {'error': 'This stream URL has already been used'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Check if user has any pending ads (required but not seen) from previous requests
            pending_ad = StreamAccess.objects.filter(user=request.user, ad_required=True, ad_seen=False).first()
            if pending_ad:
                return Response({
                    'type': 'ad',
                    'ad': AudioAdSerializer(pending_ad.ad_object).data,
                    'submit_id': pending_ad.ad_submit_id,
                    'message': 'You must finish watching the previous advertisement',
                    'pending': True
                })
            
            # Mark as unwrapped
            stream_access.unwrapped = True
            stream_access.unwrapped_at = timezone.now()
            stream_access.save(update_fields=['unwrapped', 'unwrapped_at'])
            
            # Count unwrapped streams for this user (last 24 hours for fairness)
            cutoff_time = timezone.now() - timedelta(hours=24)
            unwrapped_count = StreamAccess.objects.filter(
                user=request.user,
                unwrapped=True,
                unwrapped_at__gte=cutoff_time
            ).count()
            
            # Use ad frequency from configuration
            config = PlayConfiguration.objects.last()
            ad_freq = config.ad_frequency if config else 15
            
            if ad_freq > 0 and unwrapped_count % ad_freq == 0:
                # Pick a random active ad
                active_ads = AudioAd.objects.filter(is_active=True)
                if active_ads.exists():
                    ad = random.choice(active_ads)
                    submit_id = secrets.token_urlsafe(32)
                    
                    stream_access.ad_required = True
                    stream_access.ad_seen = False
                    stream_access.ad_submit_id = submit_id
                    stream_access.ad_object = ad
                    stream_access.save(update_fields=['ad_required', 'ad_seen', 'ad_submit_id', 'ad_object'])
                    
                    return Response({
                        'type': 'ad',
                        'ad': AudioAdSerializer(ad).data,
                        'submit_id': submit_id,
                        'message': 'Please listen to this brief advertisement',
                        'unwrap_count': unwrapped_count
                    })
            
            # No ad required, return stream response
            return UnwrapStreamView()._get_stream_response(request, stream_access, unwrapped_count)
            
        except StreamAccess.DoesNotExist:
            return Response(
                {'error': 'Invalid or unauthorized stream URL'},
                status=status.HTTP_404_NOT_FOUND
            )


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class AdSubmitView(APIView):
    """
    Endpoint to submit an ad as seen and get the final stream URL.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="ثبت مشاهده تبلیغ",
        description="تایید مشاهده تبلیغ و دریافت لینک نهایی پخش آهنگ.",
        request=inline_serializer(
            name='AdSubmitRequest',
            fields={
                'submit_id': serializers.CharField()
            }
        ),
        responses={
            200: inline_serializer(
                name='AdSubmitResponse',
                fields={
                    'type': serializers.CharField(),
                    'url': serializers.CharField(),
                    'song_id': serializers.IntegerField(),
                    'song_title': serializers.CharField(),
                    'expires_in': serializers.IntegerField(allow_null=True),
                    'unwrap_count': serializers.IntegerField(),
                    'unique_otplay_id': serializers.CharField()
                }
            )
        }
    )
    def post(self, request):
        submit_id = request.data.get('submit_id')
        if not submit_id:
            return Response({'error': 'submit_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            stream_access = StreamAccess.objects.select_related('song', 'user').get(
                ad_submit_id=submit_id, 
                user=request.user
            )
            
            if stream_access.ad_seen:
                return Response({'error': 'Ad already submitted'}, status=status.HTTP_400_BAD_REQUEST)
            
            # Mark ad as seen
            stream_access.ad_seen = True
            stream_access.save(update_fields=['ad_seen'])
            
            # Count unwrapped streams for this user (last 24 hours)
            cutoff_time = timezone.now() - timedelta(hours=24)
            unwrapped_count = StreamAccess.objects.filter(
                user=request.user,
                unwrapped=True,
                unwrapped_at__gte=cutoff_time
            ).count()
            
            # Return the final stream response
            return UnwrapStreamView()._get_stream_response(request, stream_access, unwrapped_count)

        except StreamAccess.DoesNotExist:
            return Response({'error': 'Invalid submit_id'}, status=status.HTTP_404_NOT_FOUND)


class StreamAccessView(APIView):
    """One-time access endpoint: redirects once to a presigned R2 URL and then becomes invalid."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="دسترسی یک‌باره به استریم",
        description="تولید لینک موقت و مستقیم برای پخش فایل صوتی. این لینک فقط یک بار قابل استفاده است.",
        responses={302: None}
    )
    def get(self, request, token):
        try:
            stream_access = StreamAccess.objects.select_related('song', 'user').get(
                one_time_token=token,
                user=request.user
            )

            # Check token expiry and usage
            if stream_access.one_time_used:
                return Response({'error': 'This one-time access URL has already been used'}, status=status.HTTP_400_BAD_REQUEST)

            if stream_access.one_time_expires_at and timezone.now() > stream_access.one_time_expires_at:
                return Response({'error': 'This one-time access URL has expired'}, status=status.HTTP_410_GONE)

            # Check if ad was required and seen
            if stream_access.ad_required and not stream_access.ad_seen:
                return Response({'error': 'Advertisement must be watched before accessing this stream'}, status=status.HTTP_403_FORBIDDEN)

            # Mark used before redirecting (best-effort; race-conditions remain small)
            stream_access.one_time_used = True
            stream_access.save(update_fields=['one_time_used'])

            # Build presigned R2 URL and redirect
            song = stream_access.song
            quality = request.user.settings.get('stream_quality', 'low')
            if quality == 'high' and song.audio_file:
                audio_url = song.audio_file
            elif song.converted_audio_url:
                audio_url = song.converted_audio_url
            else:
                audio_url = song.audio_file

            cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')
            if audio_url.startswith(cdn_base):
                from urllib.parse import unquote
                object_key = unquote(audio_url.replace(cdn_base + '/', ''))
            else:
                from urllib.parse import urlparse, unquote
                parsed = urlparse(audio_url)
                object_key = unquote(parsed.path.lstrip('/'))

            signed_url = generate_signed_r2_url(object_key, expiration=3600)
            from django.http import HttpResponseRedirect
            return HttpResponseRedirect(signed_url)

        except StreamAccess.DoesNotExist:
            return Response({'error': 'Invalid or unauthorized one-time token'}, status=status.HTTP_404_NOT_FOUND)


def get_client_ip(request):
    """Get the client IP address from the request."""
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class PlayCountView(APIView):
    """Endpoint to record play counts for songs."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="ثبت تعداد پخش",
        description="ثبت یک پخش جدید برای آهنگ و محاسبه درآمد هنرمند.",
        request=inline_serializer(
            name='SongStreamRecordRequest',
            fields={
                'unique_otplay_id': serializers.CharField(),
                'city': serializers.CharField(),
                'country': serializers.CharField(),
            }
        ),
        responses={
            200: inline_serializer(
                name='SongStreamRecordResponse',
                fields={
                    'message': serializers.CharField()
                }
            )
        }
    )
    def post(self, request):
        unique_otplay_id = request.data.get('unique_otplay_id')
        city = request.data.get('city')
        country = request.data.get('country')

        if not all([unique_otplay_id, city, country]):
            return Response({'error': 'unique_otplay_id, city, and country are required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            stream_access = StreamAccess.objects.get(unique_otplay_id=unique_otplay_id, user=request.user)
            if stream_access.one_time_used:
                return Response({'error': 'This play ID has already been used'}, status=status.HTTP_400_BAD_REQUEST)

            song = stream_access.song
            ip = get_client_ip(request)

            # Get latest configuration
            config = PlayConfiguration.objects.last()
            pay_value = 0.000000
            if config:
                if request.user.plan == User.PLAN_PREMIUM:
                    pay_value = config.premium_play_worth
                else:
                    pay_value = config.free_play_worth

            play_count = PlayCount.objects.create(
                user=request.user,
                country=country,
                city=city,
                ip=ip,
                pay=pay_value
            )
            song.play_counts.add(play_count)

            # Mark as used
            stream_access.one_time_used = True
            stream_access.save(update_fields=['one_time_used'])

            # Update monthly listener record for the artist
            if song.artist:
                ArtistMonthlyListener.objects.update_or_create(
                    artist=song.artist,
                    user=request.user
                )

            return Response({'message': 'Play count recorded successfully'})

        except StreamAccess.DoesNotExist:
            return Response({'error': 'Invalid unique_otplay_id'}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class UserPlaylistListCreateView(APIView):
    """List all user playlists or create a new one"""
    permission_classes = [IsAuthenticated]
    
    @extend_schema(
        summary="لیست و ایجاد پلی‌لیست‌های کاربر",
        description="دریافت لیست پلی‌لیست‌های شخصی کاربر یا ایجاد یک پلی‌لیست جدید.",
        request=UserPlaylistCreateSerializer,
        responses={200: UserPlaylistSerializer(many=True), 201: UserPlaylistSerializer}
    )
    def get(self, request):
        """List user's playlists"""
        playlists = UserPlaylist.objects.filter(user=request.user)
        serializer = UserPlaylistSerializer(playlists, many=True, context={'request': request})
        return Response(serializer.data)
    
    def post(self, request):
        """Create a new playlist, optionally with first song"""
        serializer = UserPlaylistCreateSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            playlist = serializer.save()
            response_serializer = UserPlaylistSerializer(playlist, context={'request': request})
            return Response(response_serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class UserPlaylistDetailView(APIView):
    """Retrieve, update, or delete a specific user playlist"""
    permission_classes = [IsAuthenticated]
    
    def get_object(self, pk, user):
        try:
            return UserPlaylist.objects.get(pk=pk, user=user)
        except UserPlaylist.DoesNotExist:
            return None
    
    @extend_schema(
        summary="جزئیات پلی‌لیست کاربر",
        description="مشاهده، ویرایش یا حذف یک پلی‌لیست شخصی خاص.",
        responses={200: UserPlaylistSerializer}
    )
    def get(self, request, pk):
        """Retrieve a playlist"""
        playlist = self.get_object(pk, request.user)
        if not playlist:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        serializer = UserPlaylistSerializer(playlist, context={'request': request})
        return Response(serializer.data)
    
    @extend_schema(
        summary="ویرایش پلی‌لیست کاربر",
        description="ویرایش اطلاعات پلی‌لیست شخصی (مانند نام یا تصویر).",
        request=UserPlaylistSerializer,
        responses={200: UserPlaylistSerializer}
    )
    def put(self, request, pk):
        """Update a playlist"""
        playlist = self.get_object(pk, request.user)
        if not playlist:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        serializer = UserPlaylistSerializer(playlist, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @extend_schema(
        summary="حذف پلی‌لیست کاربر",
        description="حذف کامل یک پلی‌لیست شخصی.",
        responses={204: None}
    )
    def delete(self, request, pk):
        """Delete a playlist"""
        playlist = self.get_object(pk, request.user)
        if not playlist:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        playlist.delete()
        return Response({'message': 'Playlist deleted successfully'}, status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class UserPlaylistAddSongView(APIView):
    """Add a song to a user playlist"""
    permission_classes = [IsAuthenticated]
    
    @extend_schema(
        summary="افزودن آهنگ به پلی‌لیست",
        description="اضافه کردن یک آهنگ خاص به پلی‌لیست شخصی کاربر.",
        request={
            'application/json': {
                'type': 'object',
                'properties': {
                    'song_id': {'type': 'integer', 'description': 'شناسه آهنگ'}
                },
                'required': ['song_id']
            }
        },
        responses={200: UserPlaylistSerializer}
    )
    def post(self, request, pk):
        """Add song to playlist"""
        try:
            playlist = UserPlaylist.objects.get(pk=pk, user=request.user)
        except UserPlaylist.DoesNotExist:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        
        song_id = request.data.get('song_id')
        if not song_id:
            return Response({'error': 'song_id is required'}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            song = Song.objects.get(id=song_id)
            playlist.songs.add(song)
            serializer = UserPlaylistSerializer(playlist, context={'request': request})
            return Response(serializer.data)
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class UserPlaylistRemoveSongView(APIView):
    """Remove a song from a user playlist"""
    permission_classes = [IsAuthenticated]
    
    @extend_schema(
        summary="حذف آهنگ از پلی‌لیست",
        description="حذف یک آهنگ خاص از پلی‌لیست شخصی کاربر.",
        responses={200: UserPlaylistSerializer}
    )
    def delete(self, request, pk, song_id):
        """Remove song from playlist"""
        try:
            playlist = UserPlaylist.objects.get(pk=pk, user=request.user)
        except UserPlaylist.DoesNotExist:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        
        try:
            song = Song.objects.get(id=song_id)
            playlist.songs.remove(song)
            serializer = UserPlaylistSerializer(playlist, context={'request': request})
            return Response(serializer.data)
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class HomeSummaryView(APIView):
    """
    Aggregated view for the home page summary.
    Contains recommendations, latest releases, popular artists, popular albums, and playlist recommendations.
    Optimized for speed and minimal JSON size.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="خلاصه صفحه اصلی (بهینه شده)",
        description="دریافت مجموعه‌ای از پیشنهادات، جدیدترین‌ها، هنرمندان و آلبوم‌های محبوب و پلی‌لیست‌های پیشنهادی در یک درخواست با حجم کم و سرعت بالا.",
        responses={
            200: inline_serializer(
                name='HomeSummaryResponse',
                fields={
                    'sections': serializers.IntegerField(),
                    'songs_recommendations': inline_serializer(
                        name='SongsRecSummary',
                        fields={
                            'type': serializers.CharField(),
                            'songs': SongSummarySerializer(many=True),
                        }
                    ),
                    'latest_releases': inline_serializer(
                        name='LatestRelSummary',
                        fields={
                            'count': serializers.IntegerField(),
                            'next': serializers.CharField(allow_null=True),
                            'results': SongSummarySerializer(many=True),
                        }
                    ),
                    'popular_artists': inline_serializer(
                        name='PopularArtSummary',
                        fields={
                            'count': serializers.IntegerField(),
                            'next': serializers.CharField(allow_null=True),
                            'results': ArtistSummarySerializer(many=True),
                        }
                    ),
                    'popular_albums': inline_serializer(
                        name='PopularAlbSummary',
                        fields={
                            'count': serializers.IntegerField(),
                            'next': serializers.CharField(allow_null=True),
                            'results': AlbumSummarySerializer(many=True),
                        }
                    ),
                    'playlist_recommendations': inline_serializer(
                        name='PlaylistRecSummary',
                        fields={
                            'count': serializers.IntegerField(),
                            'next': serializers.CharField(allow_null=True),
                            'results': PlaylistSummarySerializer(many=True),
                        }
                    ),
                }
            )
        }
    )
    def get(self, request, *args, **kwargs):
        user = request.user
        data = {}
        sections_count = 0
        # Per-section page sizes (smaller for heavier sections)
        section_page_sizes = {
            'songs_recommendations': 10,
            'latest_releases': 10,
            'popular_artists': 6,
            'popular_albums': 6,
            'playlist_recommendations': 6,
        }

        # helper to build pagination metadata and next link
        def build_section_meta(qs, serializer_cls, page_size, url_name, extra_params=None):
            total = qs.count()
            page = 1
            start = 0
            end = page_size
            items_qs = qs[start:end]
            items = serializer_cls(items_qs, many=True, context={'request': request}).data
            next_link = None
            if total > end:
                base_url = request.build_absolute_uri(reverse(url_name))
                params = f"?page=2&page_size={page_size}"
                if extra_params:
                    # append extra params after preserving ?
                    params += '&' + '&'.join(f"{k}={v}" for k, v in (extra_params.items()))
                next_link = f"{base_url}{params}"
            return {
                'count': page_size,
                'next': next_link,
                'results': items
            }

        # 1. Songs Recommendations (paginated)
        try:
            rec_view = UserRecommendationView()
            rec_view.force_summary = True
            rec_view.request = request
            rec_view.args = args
            rec_view.kwargs = kwargs
            rec_view.format_kwarg = None

            # Use the recommendation view as the source of truth but request only first page
            # Build a queryset-like wrapper by calling the view with page params if supported
            # Here we call the view and expect it to honor page/page_size query params.
            # Ensure the request has default page_size for summary
            # (don't mutate original request object; build a shallow copy of query_params)
            from django.http import QueryDict
            qp = request.GET.copy()
            qp._mutable = True
            qp['page'] = qp.get('page', '1')
            qp['page_size'] = qp.get('page_size', str(section_page_sizes['songs_recommendations']))
            # attach to a shallow wrapper request
            req_copy = request._request
            req_copy.GET = QueryDict(qp.urlencode())
            # call view
            rec_resp = rec_view.get(request._request)
            if hasattr(rec_resp, 'data'):
                # rec_resp.data expected to be {'type':..., 'songs':[...]} — keep as-is
                data['songs_recommendations'] = rec_resp.data
                sections_count += 1
            elif isinstance(rec_resp, dict):
                data['songs_recommendations'] = rec_resp
                sections_count += 1
            else:
                data['songs_recommendations'] = {'error': 'unexpected recommendation response', 'raw': str(rec_resp)}
        except Exception as e:
            data['songs_recommendations'] = {'error': str(e)}

        # Helper for pagination links
        def get_next_link(url_name, count, limit):
            if count >= limit: # If we reached the limit, there's likely more
                base_url = request.build_absolute_uri(reverse(url_name))
                return f"{base_url}?page=2"
            return None

        # 2. Latest Releases (paginated)
        latest_qs = Song.objects.filter(status=Song.STATUS_PUBLISHED).select_related('artist', 'album').prefetch_related('liked_by', 'genres', 'tags', 'moods', 'sub_genres', 'play_counts').order_by('-release_date', '-created_at')
        data['latest_releases'] = build_section_meta(latest_qs, SongSummarySerializer, section_page_sizes['latest_releases'], 'user_latest_releases')
        sections_count += 1

        # 3. Popular Artists (paginated)
        artist_view = PopularArtistsView()
        artist_view.request = request
        artists_qs = artist_view.get_queryset().prefetch_related('follower_artist_relations')
        data['popular_artists'] = build_section_meta(artists_qs, ArtistSummarySerializer, section_page_sizes['popular_artists'], 'user_popular_artists')
        sections_count += 1

        # 4. Popular Albums (paginated)
        album_view = PopularAlbumsView()
        album_view.request = request
        albums_qs = album_view.get_queryset().select_related('artist').prefetch_related('liked_by', 'genres', 'moods', 'sub_genres')
        data['popular_albums'] = build_section_meta(albums_qs, AlbumSummarySerializer, section_page_sizes['popular_albums'], 'user_popular_albums')
        sections_count += 1

        # 5. Playlist Recommendations (paginated)
        playlist_view = PlaylistRecommendationsView()
        playlist_view.request = request
        playlists_qs = playlist_view.get_queryset().select_related('playlist_ref').prefetch_related('songs', 'liked_by')
        data['playlist_recommendations'] = build_section_meta(playlists_qs, PlaylistSummarySerializer, section_page_sizes['playlist_recommendations'], 'user_playlist_recommendations')
        sections_count += 1

        data['sections'] = sections_count
        return Response(data)


class UserRecommendationView(APIView):
    """
    Spotify-level recommendation engine.
    Provides 10 songs based on user history (likes, plays, playlists)
    and metadata similarity.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="پیشنهادات هوشمند (مشابه اسپاتیفای)",
        description="دریافت ۱۰ آهنگ پیشنهادی بر اساس تاریخچه شنیداری، لایک‌ها و پلی‌لیست‌های کاربر با استفاده از الگوریتم شباهت.",
        responses={
            200: inline_serializer(
                name='UserRecommendationResponse',
                fields={
                    'type': serializers.CharField(),
                    'songs': SongSerializer(many=True),
                    'message': serializers.CharField(required=False),
                }
            )
        }
    )
    def get(self, request):
        user = request.user
        # Allow callers (e.g. HomeSummaryView) to force summary serializer selection
        summary_mode = getattr(self, 'force_summary', False) or (request.query_params.get('summary') == 'true')
        
        # 1. Get user's interaction history
        liked_song_ids = set(Song.objects.filter(liked_by=user).values_list('id', flat=True))
        played_song_ids = set(PlayCount.objects.filter(user=user).values_list('songs__id', flat=True))
        playlist_song_ids = set(UserPlaylist.objects.filter(user=user).values_list('songs__id', flat=True))
        
        all_interacted_ids = liked_song_ids | played_song_ids | playlist_song_ids
        
        # If no history, return trending songs
        if not all_interacted_ids:
            trending_qs = Song.objects.filter(status=Song.STATUS_PUBLISHED).select_related('artist', 'album').prefetch_related('liked_by', 'genres', 'tags', 'moods', 'sub_genres', 'play_counts').order_by('-plays')
            trending_songs = trending_qs[start:end]
            serializer_class = SongSummarySerializer if summary_mode else SongSerializer
            serializer = serializer_class(trending_songs, many=True, context={'request': request})
            return Response({
                'type': 'trending',
                'message': 'Start listening to get personalized recommendations!',
                'songs': serializer.data
            })

        # 2. Extract preferences
        interacted_songs = Song.objects.filter(id__in=all_interacted_ids)
        
        top_genres = interacted_songs.values('genres').annotate(count=Count('genres')).order_by('-count')[:3]
        top_moods = interacted_songs.values('moods').annotate(count=Count('moods')).order_by('-count')[:3]
        top_artists = interacted_songs.values('artist').annotate(count=Count('artist')).order_by('-count')[:3]
        top_languages = interacted_songs.values('language').annotate(count=Count('language')).order_by('-count')[:2]
        
        genre_ids = [g['genres'] for g in top_genres if g['genres']]
        mood_ids = [m['moods'] for m in top_moods if m['moods']]
        artist_ids = [a['artist'] for a in top_artists if a['artist']]
        preferred_languages = [l['language'] for l in top_languages if l['language']]
        
        # Average audio features
        avg_features = interacted_songs.aggregate(
            avg_energy=Avg('energy'),
            avg_dance=Avg('danceability'),
            avg_valence=Avg('valence'),
            avg_tempo=Avg('tempo')
        )

        # 3. Candidate Generation
        # Find songs that match top genres, moods, artists, or languages but haven't been interacted with
        candidates = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).exclude(
            id__in=all_interacted_ids
        ).filter(
            Q(genres__in=genre_ids) | 
            Q(moods__in=mood_ids) | 
            Q(artist__in=artist_ids) |
            Q(language__in=preferred_languages)
        ).distinct().select_related('artist', 'album').prefetch_related('genres', 'moods', 'tags', 'sub_genres', 'liked_by')
        # 4. Scoring & Ranking
        # We'll use a simple weighted scoring system in Python for better control
        scored_candidates = []
        # choose candidate limit based on requested page_size to allow multiple pages
        candidate_limit = max(200, page_size * 6)
        
        for song in candidates[:candidate_limit]:
            score = 0
            
            # Metadata matching
            song_genres = {g.id for g in song.genres.all()}
            song_moods = {m.id for m in song.moods.all()}
            
            score += len(song_genres.intersection(genre_ids)) * 3
            score += len(song_moods.intersection(mood_ids)) * 2
            if song.artist_id in artist_ids:
                score += 5
            if song.language in preferred_languages:
                score += 4  # Language match: 4 points (strong preference signal)
                
            # Audio feature similarity (inverse distance)
            if avg_features['avg_energy'] and song.energy:
                score += (100 - abs(song.energy - avg_features['avg_energy'])) / 10
            if avg_features['avg_dance'] and song.danceability:
                score += (100 - abs(song.danceability - avg_features['avg_dance'])) / 10
                
            scored_candidates.append((song, score))
            
        # Sort by score descending
        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        
        # Take top N (we'll page over the ranked list)
        ranked = [item[0] for item in scored_candidates]
        recommended_songs = ranked[start:end]
        
        # If we don't have enough recommendations, fill with trending
        if len(recommended_songs) < page_size:
            needed = page_size - len(recommended_songs)
            exclude_ids = list(all_interacted_ids) + [s.id for s in recommended_songs]
            trending = Song.objects.filter(status=Song.STATUS_PUBLISHED).exclude(
                id__in=exclude_ids
            ).order_by('-plays')[:needed]
            recommended_songs.extend(list(trending))

        serializer_class = SongSummarySerializer if summary_mode else SongSerializer
        serializer = serializer_class(recommended_songs, many=True, context={'request': request})
        return Response({
            'type': 'personalized',
            'songs': serializer.data
        })


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class LatestReleasesView(generics.ListAPIView):
    """Return songs ordered by release date (newest first), paginated with next link."""
    serializer_class = SongSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination

    @extend_schema(
        summary="جدیدترین آهنگ‌ها",
        description="دریافت لیست جدیدترین آهنگ‌های منتشر شده به ترتیب تاریخ انتشار.",
        responses={200: SongSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_permissions(self):
        # Allow unauthenticated GET access similar to other song endpoints
        if self.request.method == 'GET':
            return [permissions.AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        queryset = Song.objects.all()
        # Non-authenticated or non-staff users only see published songs
        if not self.request.user.is_authenticated or not self.request.user.is_staff:
            queryset = queryset.filter(status=Song.STATUS_PUBLISHED)

        # Order by release_date newest first, fall back to created_at for tie-breaker
        # Use NULLS LAST behavior where supported by the DB driver
        try:
            from django.db.models import F
            # annotate won't change ordering for nulls handling across DBs reliably,
            # so default to simple ordering which is acceptable in many setups
            queryset = queryset.order_by(F('release_date').desc(nulls_last=True), '-created_at')
        except Exception:
            queryset = queryset.order_by('-release_date', '-created_at')

        return queryset.distinct()


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class PopularArtistsView(generics.ListAPIView):
    """Return artists ordered by a popularity score (plays + likes + playlist adds)."""
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    serializer_class = PopularArtistSerializer

    @extend_schema(
        summary="هنرمندان محبوب",
        description="دریافت لیست هنرمندان بر اساس امتیاز محبوبیت (مجموع پخش، لایک و پلی‌لیست‌ها).",
        responses={200: PopularArtistSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_permissions(self):
        # Allow public GET access similar to other endpoints
        if self.request.method == 'GET':
            return [permissions.AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        # Annotate artists with summed metrics across their songs
        queryset = Artist.objects.all()

        # total plays across songs
        queryset = queryset.annotate(
            total_plays=Coalesce(Sum('songs__plays'), 0)
        )

        # total likes across songs (may count each like instance)
        queryset = queryset.annotate(
            total_likes=Coalesce(Count('songs__liked_by'), 0)
        )

        # items added to playlists: count occurrences in both Playlist and UserPlaylist
        queryset = queryset.annotate(
            playlists_count=Coalesce(Count('songs__playlists'), 0),
            user_playlists_count=Coalesce(Count('songs__user_playlists'), 0),
        ).annotate(
            total_playlist_adds=F('playlists_count') + F('user_playlists_count')
        )

        # combined score (simple sum) and order by it
        queryset = queryset.annotate(
            score=F('total_plays') + F('total_likes') + F('total_playlist_adds')
        ).order_by('-score', '-total_plays')

        return queryset


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class PopularAlbumsView(generics.ListAPIView):
    """Return albums ordered by combined popularity (album likes + song likes + song plays).

    Each album includes the first 3 song cover URLs (ordered by release_date, created_at).
    """
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    from .serializers import PopularAlbumSerializer
    serializer_class = PopularAlbumSerializer

    @extend_schema(
        summary="آلبوم‌های محبوب",
        description="دریافت لیست آلبوم‌های محبوب بر اساس مجموع لایک‌ها و پخش آهنگ‌های آن‌ها.",
        responses={200: PopularAlbumSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_permissions(self):
        # Public GET access
        if self.request.method == 'GET':
            return [permissions.AllowAny()]
        return super().get_permissions()

    def get_queryset(self):
        # Annotate album with song-level aggregates
        # Exclude albums literally titled "single" (case-insensitive)
        queryset = Album.objects.all().exclude(title__iexact='single')

        queryset = queryset.annotate(
            total_song_plays=Coalesce(Sum('songs__plays'), 0),
            total_song_likes=Coalesce(Count('songs__liked_by'), 0),
            # album model currently has no direct likes field; set to 0 (no model changes)
            album_likes=Value(0, output_field=IntegerField()),
            playlists_count=Coalesce(Count('songs__playlists'), 0),
            user_playlists_count=Coalesce(Count('songs__user_playlists'), 0),
        ).annotate(
            total_playlist_adds=F('playlists_count') + F('user_playlists_count')
        ).annotate(
            score=F('total_song_plays') + F('total_song_likes') + F('album_likes') + F('total_playlist_adds')
        ).order_by('-score', '-total_song_plays')

        # Prefetch songs ordered so serializer can quickly access first 3 covers
        song_prefetch = Prefetch('songs', queryset=Song.objects.order_by('-release_date', '-created_at'))
        queryset = queryset.prefetch_related(song_prefetch)

        return queryset


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class DailyTopSongsView(generics.ListAPIView):
    """Return songs ordered by play count in the last 24 hours (Global)."""
    serializer_class = SongSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    @extend_schema(
        summary="برترین آهنگ‌های روز",
        description="لیست آهنگ‌هایی که بیشترین پخش را در ۲۴ ساعت گذشته داشته‌اند.",
        responses={200: SongSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        last_24h = timezone.now() - timedelta(hours=24)
        queryset = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).annotate(
            daily_plays=Count('play_counts', filter=Q(play_counts__created_at__gte=last_24h))
        ).filter(
            daily_plays__gt=0
        ).order_by('-daily_plays', '-plays')
        
        return queryset.distinct()


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class DailyTopArtistsView(generics.ListAPIView):
    """Return artists ordered by total play count of their songs in the last 24 hours (Global)."""
    serializer_class = PopularArtistSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    @extend_schema(
        summary="برترین هنرمندان روز",
        description="لیست هنرمندانی که آثارشان بیشترین پخش را در ۲۴ ساعت گذشته داشته است.",
        responses={200: PopularArtistSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        last_24h = timezone.now() - timedelta(hours=24)
        queryset = Artist.objects.annotate(
            daily_plays=Count(
                'songs__play_counts', 
                filter=Q(songs__play_counts__created_at__gte=last_24h)
            )
        ).filter(
            daily_plays__gt=0
        ).order_by('-daily_plays')
        
        return queryset.distinct()


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class DailyTopAlbumsView(generics.ListAPIView):
    """Return albums ordered by total play count of their songs in the last 24 hours (Global)."""
    serializer_class = PopularAlbumSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    @extend_schema(
        summary="برترین آلبوم‌های روز",
        description="لیست آلبوم‌هایی که آهنگ‌های آن‌ها بیشترین پخش را در ۲۴ ساعت گذشته داشته‌اند.",
        responses={200: PopularAlbumSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        last_24h = timezone.now() - timedelta(hours=24)
        queryset = Album.objects.annotate(
            daily_plays=Count(
                'songs__play_counts', 
                filter=Q(songs__play_counts__created_at__gte=last_24h)
            )
        ).filter(
            daily_plays__gt=0
        ).order_by('-daily_plays')
        
        return queryset.distinct()


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class WeeklyTopSongsView(generics.ListAPIView):
    """Return songs ordered by play count in the last 7 days (Global)."""
    serializer_class = SongSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    @extend_schema(
        summary="برترین آهنگ‌های هفته",
        description="لیست آهنگ‌هایی که بیشترین پخش را در ۷ روز گذشته داشته‌اند.",
        responses={200: SongSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        last_week = timezone.now() - timedelta(days=7)
        # Filter songs that have at least one play in the last week
        # and annotate with the count of plays in that period
        queryset = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).annotate(
            weekly_plays=Count('play_counts', filter=Q(play_counts__created_at__gte=last_week))
        ).filter(
            weekly_plays__gt=0
        ).order_by('-weekly_plays', '-plays')
        
        return queryset.distinct()


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class WeeklyTopArtistsView(generics.ListAPIView):
    """Return artists ordered by total play count of their songs in the last 7 days (Global)."""
    serializer_class = PopularArtistSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    @extend_schema(
        summary="برترین هنرمندان هفته",
        description="لیست هنرمندانی که آثارشان بیشترین پخش را در ۷ روز گذشته داشته است.",
        responses={200: PopularArtistSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        last_week = timezone.now() - timedelta(days=7)
        # Filter artists whose songs have at least one play in the last week
        # and annotate with the count of plays in that period
        queryset = Artist.objects.annotate(
            weekly_plays=Count(
                'songs__play_counts', 
                filter=Q(songs__play_counts__created_at__gte=last_week)
            )
        ).filter(
            weekly_plays__gt=0
        ).order_by('-weekly_plays')
        
        return queryset.distinct()


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class WeeklyTopAlbumsView(generics.ListAPIView):
    """Return albums ordered by total play count of their songs in the last 7 days (Global)."""
    serializer_class = PopularAlbumSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardResultsSetPagination

    @extend_schema(
        summary="برترین آلبوم‌های هفته",
        description="لیست آلبوم‌هایی که آهنگ‌های آن‌ها بیشترین پخش را در ۷ روز گذشته داشته‌اند.",
        responses={200: PopularAlbumSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        last_week = timezone.now() - timedelta(days=7)
        # Filter albums whose songs have at least one play in the last week
        # and annotate with the count of plays in that period
        queryset = Album.objects.annotate(
            weekly_plays=Count(
                'songs__play_counts', 
                filter=Q(songs__play_counts__created_at__gte=last_week)
            )
        ).filter(
            weekly_plays__gt=0
        ).order_by('-weekly_plays')
        
        return queryset.distinct()


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class PlaylistRecommendationsView(generics.ListAPIView):
    """
    Auto-generate and return personalized playlist recommendations.
    Based on user activity (plays, likes, playlists).
    Returns existing playlists and generates new ones.
    """
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination
    from .serializers import RecommendedPlaylistListSerializer
    serializer_class = RecommendedPlaylistListSerializer

    @extend_schema(
        summary="پیشنهادات پلی‌لیست",
        description="دریافت پلی‌لیست‌های پیشنهادی هوشمند بر اساس سلیقه کاربر. این لیست هر ۳ درخواست یکبار به‌روزرسانی می‌شود.",
        responses={200: RecommendedPlaylistListSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        user = self.request.user
        
        # Note: generation is triggered in `list()` (every 3 requests).
        # Here we only return cached/available recommendations.
        
        # Return all recommendations for this user, sorted by relevance
        from .models import RecommendedPlaylist
        queryset = RecommendedPlaylist.objects.filter(
            Q(user=user) | Q(user__isnull=True)  # User-specific or general recommendations
        ).filter(
            Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())  # Not expired
        ).prefetch_related('songs', 'liked_by', 'saved_by')
        
        return queryset

    def list(self, request, *args, **kwargs):
        """Override list to count requests and regenerate every 3rd request."""
        user = request.user

        # increment a lightweight counter in user.settings
        try:
            settings_json = user.settings or {}
        except Exception:
            settings_json = {}

        cnt = int(settings_json.get('recommendations_request_count', 0)) + 1
        settings_json['recommendations_request_count'] = cnt
        user.settings = settings_json
        try:
            user.save(update_fields=['settings'])
        except Exception:
            user.save()

        # Regenerate when counter divisible by 3
        if cnt % 3 == 0:
            self._generate_recommendations(user, force=True)

        return super().list(request, *args, **kwargs)


    def _generate_recommendations(self, user, force=False):
        """Generate personalized playlist recommendations based on user activity"""
        from .models import RecommendedPlaylist
        import hashlib
        
        # Check if we have recent recommendations (generated in last 6 hours)
        recent_cutoff = timezone.now() - timedelta(hours=6)
        recent_count = RecommendedPlaylist.objects.filter(
            user=user,
            created_at__gte=recent_cutoff
        ).count()

        # If we have recent recommendations and not forced, skip generation
        if not force and recent_count >= 6:
            return
        
        # 1. Get user's interaction history
        liked_song_ids = set(Song.objects.filter(liked_by=user).values_list('id', flat=True))
        played_song_ids = set(PlayCount.objects.filter(user=user).values_list('songs__id', flat=True))
        playlist_song_ids = set(UserPlaylist.objects.filter(user=user).values_list('songs__id', flat=True))
        
        all_interacted_ids = liked_song_ids | played_song_ids | playlist_song_ids
        
        # If no history, generate general trending playlists
        if not all_interacted_ids:
            self._generate_trending_playlists(user)
            return
        
        # 2. Extract preferences
        interacted_songs = Song.objects.filter(id__in=all_interacted_ids)
        
        # Get top preferences
        top_genres = list(interacted_songs.values_list('genres', flat=True).distinct())[:5]
        top_moods = list(interacted_songs.values_list('moods', flat=True).distinct())[:5]
        top_artists = list(interacted_songs.values_list('artist', flat=True).distinct())[:5]
        
        # Average audio features
        avg_features = interacted_songs.aggregate(
            avg_energy=Avg('energy'),
            avg_dance=Avg('danceability'),
            avg_valence=Avg('valence'),
            avg_tempo=Avg('tempo')
        )
        
        # 3. Generate different types of playlists
        generated_playlists = []
        
        # A. Similar Taste Playlist - Songs close to what they already like
        similar_playlist = self._create_similar_taste_playlist(user, all_interacted_ids, top_genres, top_moods, avg_features)
        if similar_playlist:
            generated_playlists.append(similar_playlist)
        
        # B. Discover Genre Playlists - Explore genres they haven't tried much
        genre_playlists = self._create_genre_discovery_playlists(user, all_interacted_ids, top_genres)
        generated_playlists.extend(genre_playlists)
        
        # C. Mood-Based Playlists
        mood_playlists = self._create_mood_playlists(user, all_interacted_ids, top_moods, avg_features)
        generated_playlists.extend(mood_playlists)
        
        # D. Energy Level Playlists
        energy_playlists = self._create_energy_playlists(user, all_interacted_ids, avg_features)
        generated_playlists.extend(energy_playlists)
        
        # E. Artist Mix Playlists
        artist_playlists = self._create_artist_mix_playlists(user, all_interacted_ids, top_artists)
        generated_playlists.extend(artist_playlists)
        
        # F. If we don't have enough playlists, add general cohesive playlists
        if len(generated_playlists) < 6:
            general_playlists = self._create_general_cohesive_playlists(user, all_interacted_ids, 6 - len(generated_playlists))
            generated_playlists.extend(general_playlists)
        
        # Save all generated playlists (ensure at least 6, max 12)
        for playlist_data in generated_playlists[:12]:
            self._save_playlist(user, playlist_data)

    def _create_similar_taste_playlist(self, user, excluded_ids, top_genres, top_moods, avg_features):
        """Create a playlist similar to user's current taste"""
        candidates = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).exclude(
            id__in=excluded_ids
        ).filter(
            Q(genres__in=top_genres) | Q(moods__in=top_moods)
        ).distinct().prefetch_related('genres', 'moods')[:200]
        
        # Score songs by similarity
        scored_songs = self._score_songs_by_similarity(candidates, top_genres, top_moods, avg_features)
        
        if len(scored_songs) < 10:
            return None
        
        selected_songs = [s[0] for s in scored_songs[:20]]
        
        # Calculate match percentage based on average score
        avg_score = sum(s[1] for s in scored_songs[:20]) / len(scored_songs[:20])
        match_percentage = min(100.0, (avg_score / 20) * 100)  # Normalize to 0-100
        
        import hashlib
        unique_id = hashlib.sha256(f"similar_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
        
        return {
            'unique_id': unique_id,
            'title': 'More of What You Love',
            'description': 'Songs similar to your favorites',
            'playlist_type': 'similar_taste',
            'songs': selected_songs,
            'relevance_score': 10.0,
            'match_percentage': round(match_percentage, 1)
        }

    def _create_genre_discovery_playlists(self, user, excluded_ids, top_genres):
        """Create playlists to help discover new genres"""
        from .models import Genre
        
        playlists = []
        
        # Get genres user hasn't explored much
        all_genres = Genre.objects.exclude(id__in=top_genres)[:3]
        
        for genre in all_genres:
            songs = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                genres=genre
            ).exclude(
                id__in=excluded_ids
            ).order_by('-plays')[:20]
            
            if songs.count() >= 10:
                import hashlib
                unique_id = hashlib.sha256(f"discover_{genre.id}_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                
                playlists.append({
                    'unique_id': unique_id,
                    'title': f'Discover {genre.name}',
                    'description': f'Explore popular {genre.name} tracks',
                    'playlist_type': 'discover_genre',
                    'songs': list(songs),
                    'relevance_score': 7.0,
                    'match_percentage': 0.0  # Discovery playlists don't match existing taste
                })
        
        return playlists[:3]  # Max 3 discovery playlists

    def _create_mood_playlists(self, user, excluded_ids, top_moods, avg_features):
        """Create mood-based playlists"""
        from .models import Mood
        
        playlists = []
        
        # Get user's favorite moods
        moods = Mood.objects.filter(id__in=top_moods)[:2]
        
        for mood in moods:
            songs = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                moods=mood
            ).exclude(
                id__in=excluded_ids
            )
            
            # Filter by similar audio features
            if avg_features['avg_valence']:
                songs = songs.filter(
                    valence__gte=avg_features['avg_valence'] - 20,
                    valence__lte=avg_features['avg_valence'] + 20
                )
            
            songs = songs.order_by('-plays')[:20]
            
            if songs.count() >= 10:
                import hashlib
                unique_id = hashlib.sha256(f"mood_{mood.id}_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                
                playlists.append({
                    'unique_id': unique_id,
                    'title': f'{mood.name} Vibes',
                    'description': f'Perfect songs for a {mood.name.lower()} mood',
                    'playlist_type': 'mood_based',
                    'songs': list(songs),
                    'relevance_score': 8.5,
                    'match_percentage': 75.0  # Mood-based matches user's mood preferences
                })
        
        return playlists

    def _create_energy_playlists(self, user, excluded_ids, avg_features):
        """Create playlists based on energy levels"""
        playlists = []
        
        if not avg_features['avg_energy']:
            return playlists
        
        # High energy playlist
        high_energy_songs = Song.objects.filter(
            status=Song.STATUS_PUBLISHED,
            energy__gte=70
        ).exclude(
            id__in=excluded_ids
        ).order_by('-energy', '-plays')[:20]
        
        if high_energy_songs.count() >= 10:
            import hashlib
            unique_id = hashlib.sha256(f"high_energy_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
            
            playlists.append({
                'unique_id': unique_id,
                'title': 'High Energy Boost',
                'description': 'Pump up the energy with these tracks',
                'playlist_type': 'energy',
                'songs': list(high_energy_songs),
                'relevance_score': 7.5,
                'match_percentage': 60.0  # Energy-based has moderate match
            })
        
        # Chill/Relaxing playlist
        chill_songs = Song.objects.filter(
            status=Song.STATUS_PUBLISHED,
            energy__lte=40,
            acousticness__gte=30
        ).exclude(
            id__in=excluded_ids
        ).order_by('energy', '-plays')[:20]
        
        if chill_songs.count() >= 10:
            import hashlib
            unique_id = hashlib.sha256(f"chill_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
            
            playlists.append({
                'unique_id': unique_id,
                'title': 'Chill & Relax',
                'description': 'Unwind with these mellow tracks',
                'playlist_type': 'energy',
                'songs': list(chill_songs),
                'relevance_score': 7.5,
                'match_percentage': 60.0  # Energy-based has moderate match
            })
        
        return playlists  # Return all energy playlists (up to 2)

    def _create_artist_mix_playlists(self, user, excluded_ids, top_artists):
        """Create playlists mixing songs from favorite artists"""
        from .models import Artist
        
        playlists = []
        
        if len(top_artists) < 2:
            return playlists
        
        # Get songs from top 3 artists
        artists = Artist.objects.filter(id__in=top_artists[:3])
        
        songs = Song.objects.filter(
            status=Song.STATUS_PUBLISHED,
            artist__in=artists
        ).exclude(
            id__in=excluded_ids
        ).order_by('-plays', '-release_date')[:20]
        
        if songs.count() >= 10:
            artist_names = ', '.join([a.name for a in artists[:2]])
            import hashlib
            unique_id = hashlib.sha256(f"artist_mix_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
            
            playlists.append({
                'unique_id': unique_id,
                'title': f'{artist_names} & More',
                'description': f'A mix featuring your favorite artists',
                'playlist_type': 'artist_mix',
                'songs': list(songs),
                'relevance_score': 9.0,
                'match_percentage': 85.0  # Artist mix has high match with user taste
            })
        
        return playlists

    def _generate_trending_playlists(self, user):
        """Generate general trending playlists for users without history"""
        import hashlib
        
        playlists = []
        
        # Trending overall
        trending_songs = Song.objects.filter(
            status=Song.STATUS_PUBLISHED
        ).order_by('-plays')[:20]
        
        if trending_songs.count() >= 10:
            unique_id = hashlib.sha256(f"trending_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
            playlists.append({
                'unique_id': unique_id,
                'title': 'Trending Now',
                'description': 'Most popular tracks right now',
                'playlist_type': 'similar_taste',
                'songs': list(trending_songs),
                'relevance_score': 5.0,
                'match_percentage': 0.0
            })
        
        # New releases
        new_releases = Song.objects.filter(
            status=Song.STATUS_PUBLISHED,
            release_date__isnull=False
        ).order_by('-release_date')[:20]
        
        if new_releases.count() >= 10:
            unique_id = hashlib.sha256(f"new_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
            playlists.append({
                'unique_id': unique_id,
                'title': 'Fresh Releases',
                'description': 'Newest tracks just for you',
                'playlist_type': 'similar_taste',
                'songs': list(new_releases),
                'relevance_score': 5.0,
                'match_percentage': 0.0
            })
        
        # Add general cohesive playlists to reach at least 6
        if len(playlists) < 6:
            general_playlists = self._create_general_cohesive_playlists(user, set(), 6 - len(playlists))
            playlists.extend(general_playlists)
        
        # Save all playlists
        for playlist_data in playlists:
            self._save_playlist(user, playlist_data)

    def _create_general_cohesive_playlists(self, user, excluded_ids, count_needed):
        """Create general playlists that are cohesive but not user-specific"""
        from .models import Genre, Mood
        import hashlib
        
        playlists = []
        
        # 1. High Energy Workout
        if count_needed > 0:
            high_energy = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                energy__gte=75,
                danceability__gte=60
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if high_energy.count() >= 10:
                unique_id = hashlib.sha256(f"gen_energy_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Power Workout',
                    'description': 'High-energy tracks to fuel your workout',
                    'playlist_type': 'energy',
                    'songs': list(high_energy),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 2. Acoustic & Chill
        if len(playlists) < count_needed:
            acoustic = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                acousticness__gte=60,
                energy__lte=50
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if acoustic.count() >= 10:
                unique_id = hashlib.sha256(f"gen_acoustic_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Acoustic Sessions',
                    'description': 'Mellow acoustic vibes for relaxation',
                    'playlist_type': 'mood_based',
                    'songs': list(acoustic),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 3. Happy & Upbeat
        if len(playlists) < count_needed:
            happy = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                valence__gte=70,
                energy__gte=60
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if happy.count() >= 10:
                unique_id = hashlib.sha256(f"gen_happy_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Feel Good Hits',
                    'description': 'Upbeat songs to brighten your day',
                    'playlist_type': 'mood_based',
                    'songs': list(happy),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 4. Melancholic & Dramatic
        if len(playlists) < count_needed:
            dramatic = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                valence__lte=40,
                energy__lte=60
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if dramatic.count() >= 10:
                unique_id = hashlib.sha256(f"gen_dramatic_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Emotional Journey',
                    'description': 'Deep, emotional tracks for reflective moments',
                    'playlist_type': 'mood_based',
                    'songs': list(dramatic),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 5. Dance Party
        if len(playlists) < count_needed:
            dance = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                danceability__gte=75
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if dance.count() >= 10:
                unique_id = hashlib.sha256(f"gen_dance_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Dance Floor Fillers',
                    'description': 'Get moving with these danceable tracks',
                    'playlist_type': 'energy',
                    'songs': list(dance),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 6. Focus & Study
        if len(playlists) < count_needed:
            focus = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                instrumentalness__gte=50,
                energy__lte=50
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if focus.count() >= 10:
                unique_id = hashlib.sha256(f"gen_focus_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Focus & Productivity',
                    'description': 'Instrumental tracks for concentration',
                    'playlist_type': 'mood_based',
                    'songs': list(focus),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 7. Late Night Vibes
        if len(playlists) < count_needed:
            night = Song.objects.filter(
                status=Song.STATUS_PUBLISHED,
                energy__lte=45,
                valence__range=(30, 60)
            ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
            
            if night.count() >= 10:
                unique_id = hashlib.sha256(f"gen_night_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                playlists.append({
                    'unique_id': unique_id,
                    'title': 'Late Night Vibes',
                    'description': 'Smooth tracks for the evening',
                    'playlist_type': 'mood_based',
                    'songs': list(night),
                    'relevance_score': 6.0,
                    'match_percentage': 0.0
                })
        
        # 8. Genre-based fallbacks
        if len(playlists) < count_needed:
            all_genres = Genre.objects.all()[:5]
            for genre in all_genres:
                if len(playlists) >= count_needed:
                    break
                    
                genre_songs = Song.objects.filter(
                    status=Song.STATUS_PUBLISHED,
                    genres=genre
                ).exclude(id__in=excluded_ids).order_by('-plays')[:20]
                
                if genre_songs.count() >= 10:
                    unique_id = hashlib.sha256(f"gen_genre_{genre.id}_{user.id}_{timezone.now().date()}".encode()).hexdigest()[:32]
                    playlists.append({
                        'unique_id': unique_id,
                        'title': f'Best of {genre.name}',
                        'description': f'Top {genre.name} tracks',
                        'playlist_type': 'discover_genre',
                        'songs': list(genre_songs),
                        'relevance_score': 5.5,
                        'match_percentage': 0.0
                    })
        
        return playlists[:count_needed]

    def _score_songs_by_similarity(self, songs, top_genres, top_moods, avg_features):
        """Score songs by similarity to user preferences"""
        scored = []
        
        for song in songs:
            score = 0
            
            # Genre matching
            song_genres = {g.id for g in song.genres.all()}
            score += len(song_genres.intersection(top_genres)) * 3
            
            # Mood matching
            song_moods = {m.id for m in song.moods.all()}
            score += len(song_moods.intersection(top_moods)) * 2
            
            # Audio feature similarity
            if avg_features['avg_energy'] and song.energy:
                score += max(0, (100 - abs(song.energy - avg_features['avg_energy'])) / 10)
            if avg_features['avg_dance'] and song.danceability:
                score += max(0, (100 - abs(song.danceability - avg_features['avg_dance'])) / 10)
            if avg_features['avg_valence'] and song.valence:
                score += max(0, (100 - abs(song.valence - avg_features['avg_valence'])) / 10)
            
            # Popularity boost (but not too much)
            score += min(song.plays / 1000, 5)
            
            scored.append((song, score))
        
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def _save_playlist(self, user, playlist_data):
        """Save or update a recommended playlist"""
        from .models import RecommendedPlaylist, Playlist
        
        # Check if playlist with this unique_id already exists
        existing = RecommendedPlaylist.objects.filter(
            unique_id=playlist_data['unique_id']
        ).first()
        
        # Create or update the canonical Playlist object
        if existing and existing.playlist_ref:
            pl = existing.playlist_ref
            pl.title = playlist_data['title']
            pl.description = playlist_data['description']
            pl.save()
        else:
            pl = Playlist.objects.create(
                title=playlist_data['title'],
                description=playlist_data['description'],
                created_by=Playlist.CREATED_BY_SYSTEM
            )
        
        # Sync songs to the canonical playlist
        pl.songs.set(playlist_data['songs'])
        
        if existing:
            # Update existing
            existing.title = playlist_data['title']
            existing.description = playlist_data['description']
            existing.relevance_score = playlist_data['relevance_score']
            existing.match_percentage = playlist_data.get('match_percentage', 0.0)
            existing.updated_at = timezone.now()
            existing.expires_at = timezone.now() + timedelta(days=7)
            existing.playlist_ref = pl
            # Update songs: clear/add then set a randomized explicit order
            existing.songs.clear()
            existing.songs.add(*playlist_data['songs'])
            try:
                song_ids = [s.id for s in playlist_data['songs']]
            except Exception:
                song_ids = []
            # Shuffle to ensure list view doesn't look identical each time
            if song_ids:
                random.shuffle(song_ids)
                existing.song_order = song_ids
            existing.save()
        else:
            # Create new
            playlist = RecommendedPlaylist.objects.create(
                unique_id=playlist_data['unique_id'],
                user=user,
                title=playlist_data['title'],
                description=playlist_data['description'],
                playlist_type=playlist_data['playlist_type'],
                relevance_score=playlist_data['relevance_score'],
                match_percentage=playlist_data.get('match_percentage', 0.0),
                expires_at=timezone.now() + timedelta(days=7),
                playlist_ref=pl
            )
            playlist.songs.add(*playlist_data['songs'])
            try:
                song_ids = [s.id for s in playlist_data['songs']]
            except Exception:
                song_ids = []
            if song_ids:
                random.shuffle(song_ids)
                playlist.song_order = song_ids
                playlist.save()


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class PlaylistRecommendationDetailView(generics.RetrieveAPIView):
    """
    Detail view for a specific recommended playlist.
    Shows all songs without stream links.
    Increments view count.
    """
    permission_classes = [IsAuthenticated]
    from .serializers import RecommendedPlaylistDetailSerializer
    serializer_class = RecommendedPlaylistDetailSerializer
    lookup_field = 'unique_id'

    @extend_schema(
        summary="جزئیات پلی‌لیست پیشنهادی",
        description="مشاهده جزئیات و لیست آهنگ‌های یک پلی‌لیست پیشنهادی خاص.",
        responses={200: RecommendedPlaylistDetailSerializer}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        from .models import RecommendedPlaylist
        return RecommendedPlaylist.objects.all().prefetch_related(
            'songs__artist',
            'songs__album',
            'liked_by',
            'saved_by'
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        
        # Increment view count and record user
        instance.views += 1
        if request.user.is_authenticated:
            instance.viewed_by.add(request.user)
        instance.save(update_fields=['views'])
        
        serializer = self.get_serializer(instance)
        return Response(serializer.data)


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class PlaylistRecommendationLikeView(APIView):
    """Like or unlike a recommended playlist"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لایک کردن پلی‌لیست پیشنهادی",
        description="لایک کردن یا لغو لایک یک پلی‌لیست پیشنهادی.",
        responses={
            200: inline_serializer(
                name='PlaylistRecommendationLikeResponse',
                fields={
                    'status': serializers.CharField(),
                    'likes_count': serializers.IntegerField(),
                }
            )
        }
    )
    def post(self, request, unique_id):
        from .models import RecommendedPlaylist
        
        try:
            playlist = RecommendedPlaylist.objects.get(unique_id=unique_id)
        except RecommendedPlaylist.DoesNotExist:
            return Response(
                {'error': 'Playlist not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        if playlist.liked_by.filter(id=request.user.id).exists():
            # Unlike
            playlist.liked_by.remove(request.user)
            return Response({'status': 'unliked', 'likes_count': playlist.liked_by.count()})
        else:
            # Like
            playlist.liked_by.add(request.user)
            return Response({'status': 'liked', 'likes_count': playlist.liked_by.count()})


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class PlaylistRecommendationSaveView(APIView):
    """Save or unsave a recommended playlist"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="ذخیره کردن پلی‌لیست پیشنهادی",
        description="ذخیره کردن یا لغو ذخیره یک پلی‌لیست پیشنهادی در کتابخانه کاربر.",
        responses={
            200: inline_serializer(
                name='PlaylistRecommendationSaveResponse',
                fields={
                    'status': serializers.CharField(),
                }
            )
        }
    )
    def post(self, request, unique_id):
        from .models import RecommendedPlaylist
        
        try:
            playlist = RecommendedPlaylist.objects.get(unique_id=unique_id)
        except RecommendedPlaylist.DoesNotExist:
            return Response(
                {'error': 'Playlist not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        if playlist.saved_by.filter(id=request.user.id).exists():
            # Unsave
            playlist.saved_by.remove(request.user)
            return Response({'status': 'unsaved'})
        else:
            # Save
            playlist.saved_by.add(request.user)
            return Response({'status': 'saved'})


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و  صفحات جزئیات و عملیات'])
class PlaylistSaveToggleView(APIView):
    """Toggle save/unsave for canonical Playlist objects"""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="ذخیره کردن پلی‌لیست",
        description="ذخیره کردن یا لغو ذخیره یک پلی‌لیست عمومی در کتابخانه کاربر.",
        responses={
            200: inline_serializer(
                name='PlaylistSaveToggleResponse',
                fields={
                    'status': serializers.CharField(),
                }
            )
        }
    )
    def post(self, request, pk, *args, **kwargs):
        try:
            playlist = Playlist.objects.get(id=pk)
        except Playlist.DoesNotExist:
            return Response({'detail': 'playlist not found'}, status=status.HTTP_404_NOT_FOUND)

        user = request.user
        if playlist.saved_by.filter(id=user.id).exists():
            playlist.saved_by.remove(user)
            return Response({'status': 'unsaved'}, status=status.HTTP_200_OK)
        else:
            playlist.saved_by.add(user)
            return Response({'status': 'saved'}, status=status.HTTP_200_OK)


@extend_schema(tags=['Search Page Endpoints اندپوینت های صفحه جستجو'])
class SearchView(APIView):
    """
    Unified search endpoint for songs, artists, albums, and playlists.
    Supports complex matching and mixed results.
    """
    permission_classes = [AllowAny]
    
    @extend_schema(
        summary="جستجوی جامع",
        description="جستجو در آهنگ‌ها، هنرمندان، آلبوم‌ها و پلی‌لیست‌ها با قابلیت فیلتر بر اساس سبک و حال و هوا.",
        parameters=[
            OpenApiParameter("q", OpenApiTypes.STR, description="متن جستجو"),
            OpenApiParameter("type", OpenApiTypes.STR, description="نوع جستجو (song, artist, album, playlist)"),
            OpenApiParameter("moods", OpenApiTypes.STR, description="فیلتر بر اساس حال و هوا (آیدی یا اسلاگ)", many=True),
            OpenApiParameter("page", OpenApiTypes.INT, description="شماره صفحه"),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="تعداد در هر صفحه")
        ],
        responses={
            200: inline_serializer(
                name='SearchResponse',
                fields={
                    'results': SearchResultSerializer(many=True),
                    'page': serializers.IntegerField(),
                    'page_size': serializers.IntegerField(),
                    'has_next': serializers.BooleanField(),
                    'query': serializers.CharField(),
                    'moods': serializers.ListField(child=serializers.CharField()),
                    'type': serializers.CharField(),
                }
            )
        }
    )
    def get(self, request):
        q = request.query_params.get('q', '').strip()
        search_type = request.query_params.get('type')
        moods = request.query_params.getlist('moods')
        page = int(request.query_params.get('page', 1))
        page_size = int(request.query_params.get('page_size', 20))
        
        offset = (page - 1) * page_size
        results = []
        
        if search_type:
            # Single type search
            if search_type == 'song':
                items = self._search_songs(q, moods)
            elif search_type == 'artist':
                items = self._search_artists(q)
            elif search_type == 'album':
                items = self._search_albums(q)
            elif search_type == 'playlist':
                items = self._search_playlists(q, moods)
            else:
                return Response({'error': 'Invalid type. Must be song, artist, album, or playlist.'}, status=400)
            
            paginated_items = items[offset:offset + page_size]
            results = list(paginated_items)
            has_next = items.count() > offset + page_size
        else:
            # Mixed search (interleaved)
            per_type = page_size // 4
            
            songs = list(self._search_songs(q, moods)[offset:offset + per_type])
            artists = list(self._search_artists(q)[offset:offset + per_type])
            albums = list(self._search_albums(q)[offset:offset + per_type])
            playlists = list(self._search_playlists(q, moods)[offset:offset + per_type])
            
            # Interleave results
            max_len = max(len(songs), len(artists), len(albums), len(playlists))
            for i in range(max_len):
                if i < len(songs): results.append(songs[i])
                if i < len(artists): results.append(artists[i])
                if i < len(albums): results.append(albums[i])
                if i < len(playlists): results.append(playlists[i])
            
            # For mixed, we assume there's more if we got a full page
            has_next = len(results) >= page_size

        serializer = SearchResultSerializer(results, many=True, context={'request': request})
        
        return Response({
            'results': serializer.data,
            'page': page,
            'page_size': page_size,
            'has_next': has_next,
            'query': q,
            'moods': moods,
            'type': search_type or 'mixed'
        })

    def _search_songs(self, q, moods=None):
        qs = Song.objects.filter(status=Song.STATUS_PUBLISHED).select_related('artist', 'album')
        if q:
            # Complex matching: title, description, lyrics, producers, composers, lyricists, artist name, album title
            qs = qs.filter(
                Q(title__icontains=q) |
                Q(description__icontains=q) |
                Q(lyrics__icontains=q) |
                Q(producers__icontains=q) |
                Q(composers__icontains=q) |
                Q(lyricists__icontains=q) |
                Q(artist__name__icontains=q) |
                Q(album__title__icontains=q)
            )
        
        if moods:
            # Filter by mood IDs or slugs
            if all(m.isdigit() for m in moods):
                qs = qs.filter(moods__id__in=moods).distinct()
            else:
                qs = qs.filter(moods__slug__in=moods).distinct()
                
        return qs.order_by('-plays', '-created_at')

    def _search_artists(self, q):
        qs = Artist.objects.all()
        if q:
            qs = qs.filter(
                Q(name__icontains=q) |
                Q(bio__icontains=q)
            )
        return qs.order_by('-verified', '-created_at')

    def _search_albums(self, q):
        qs = Album.objects.all().select_related('artist')
        if q:
            qs = qs.filter(
                Q(title__icontains=q) |
                Q(description__icontains=q) |
                Q(artist__name__icontains=q)
            )
        return qs.order_by('-release_date')

    def _search_playlists(self, q, moods=None):
        # Combine admin/system playlists and public user playlists
        admin_qs = Playlist.objects.all()
        if q:
            admin_qs = admin_qs.filter(
                Q(title__icontains=q) |
                Q(description__icontains=q)
            )
        
        if moods:
            if all(m.isdigit() for m in moods):
                admin_qs = admin_qs.filter(moods__id__in=moods).distinct()
            else:
                admin_qs = admin_qs.filter(moods__slug__in=moods).distinct()
                
        return admin_qs.order_by('-created_at')


@extend_schema(tags=['Search Page Endpoints اندپوینت های صفحه جستجو'])
class EventPlaylistView(APIView):
    """Return event playlist groups with all details."""
    permission_classes = [permissions.AllowAny]
    
    @extend_schema(
        summary="پلی‌لیست‌های مناسبتی",
        description="دریافت گروه‌های پلی‌لیست مناسبتی (مانند پلی‌لیست‌های صبحگاهی، شبانه و غیره).",
        parameters=[
            OpenApiParameter("time_of_day", OpenApiTypes.STR, description="فیلتر بر اساس زمان روز")
        ],
        responses={200: EventPlaylistSerializer(many=True)}
    )
    def get(self, request):
        queryset = EventPlaylist.objects.all().prefetch_related(
            'playlists', 
            'playlists__songs',
            'playlists__songs__artist',
            'playlists__genres',
            'playlists__moods'
        )
        
        time_of_day = request.query_params.get('time_of_day')
        if time_of_day:
            queryset = queryset.filter(time_of_day=time_of_day)
            
        serializer = EventPlaylistSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)


@extend_schema(tags=['Search Page Endpoints اندپوینت های صفحه جستجو'])
class SearchSectionListView(APIView):
    """List and Create SearchSections"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    @extend_schema(
        summary="لیست بخش‌های جستجو",
        description="دریافت لیست بخش‌های مختلف صفحه جستجو (مانند دسته‌بندی‌ها).",
        responses={200: SearchSectionSerializer(many=True)}
    )
    def get(self, request):
        sections = SearchSection.objects.all().prefetch_related('songs', 'albums', 'playlists', 'songs__artist', 'albums__artist')
        serializer = SearchSectionSerializer(sections, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ایجاد بخش جستجو",
        description="ایجاد یک بخش جدید برای صفحه جستجو (فقط برای کاربران احراز هویت شده).",
        request=SearchSectionSerializer,
        responses={201: SearchSectionSerializer}
    )
    def post(self, request):
        serializer = SearchSectionSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save(created_by=request.user, updated_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Search Page Endpoints اندپوینت های صفحه جستجو'])
class SearchSectionDetailView(APIView):
    """Retrieve, Update, and Delete SearchSection"""
    def get_permissions(self):
        if self.request.method == 'GET':
            return [AllowAny()]
        return [IsAuthenticated()]

    def get_object(self, pk):
        try:
            return SearchSection.objects.get(pk=pk)
        except SearchSection.DoesNotExist:
            return None

    @extend_schema(
        summary="جزئیات بخش جستجو",
        description="دریافت اطلاعات کامل یک بخش خاص از صفحه جستجو.",
        responses={200: SearchSectionSerializer}
    )
    def get(self, request, pk):
        section = self.get_object(pk)
        if not section:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SearchSectionSerializer(section, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کامل بخش جستجو",
        description="ویرایش تمامی اطلاعات یک بخش جستجو.",
        request=SearchSectionSerializer,
        responses={200: SearchSectionSerializer}
    )
    def put(self, request, pk):
        section = self.get_object(pk)
        if not section:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SearchSectionSerializer(section, data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save(updated_by=request.user)
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="ویرایش جزئی بخش جستجو",
        description="ویرایش برخی از اطلاعات یک بخش جستجو.",
        request=SearchSectionSerializer,
        responses={200: SearchSectionSerializer}
    )
    def patch(self, request, pk):
        section = self.get_object(pk)
        if not section:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = SearchSectionSerializer(section, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save(updated_by=request.user)
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف بخش جستجو",
        description="حذف یک بخش از صفحه جستجو.",
        responses={204: None}
    )
    def delete(self, request, pk):
        section = self.get_object(pk)
        if not section:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        section.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class RulesListCreateView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست قوانین (ادمین)",
        description="دریافت لیست تمامی قوانین ثبت شده در سیستم.",
        responses={200: RulesSerializer(many=True)}
    )
    def get(self, request):
        rules = Rules.objects.all()
        serializer = RulesSerializer(rules, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="ایجاد قانون جدید (ادمین)",
        description="ثبت یک قانون جدید در سیستم.",
        request=RulesSerializer,
        responses={201: RulesSerializer}
    )
    def post(self, request):
        serializer = RulesSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)




@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class RulesDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="جزئیات قانون (ادمین)",
        description="دریافت جزئیات یک قانون خاص.",
        responses={
            200: RulesSerializer,
            404: inline_serializer(name='RuleNotFound', fields={'detail': serializers.CharField()})
        }
    )
    def get(self, request, pk):
        try:
            rule = Rules.objects.get(pk=pk)
        except Rules.DoesNotExist:
            return Response({"detail": "Rule not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = RulesSerializer(rule)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش قانون (ادمین)",
        description="ویرایش یک قانون موجود.",
        request=RulesSerializer,
        responses={
            200: RulesSerializer,
            404: inline_serializer(name='RuleNotFoundEdit', fields={'detail': serializers.CharField()}),
            400: inline_serializer(name='RuleBadRequest', fields={'detail': serializers.CharField()})
        }
    )
    def put(self, request, pk):
        try:
            rule = Rules.objects.get(pk=pk)
        except Rules.DoesNotExist:
            return Response({"detail": "Rule not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = RulesSerializer(rule, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف قانون (ادمین)",
        description="حذف یک قانون موجود.",
        responses={
            204: None,
            404: inline_serializer(name='RuleNotFoundDelete', fields={'detail': serializers.CharField()})
        }
    )
    def delete(self, request, pk):
        try:
            rule = Rules.objects.get(pk=pk)
        except Rules.DoesNotExist:
            return Response({"detail": "Rule not found."}, status=status.HTTP_404_NOT_FOUND)
        rule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)




@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و  صفحات جزئیات و عملیات'])
class RulesLatestView(APIView):
    """Return the latest Rules entry (single item) for public consumption.
    Accessible by both audience and artists.
    """
    permission_classes = [AllowAny]

    @extend_schema(
        summary="آخرین قوانین",
        description="دریافت آخرین نسخه قوانین و مقررات پلتفرم.",
        responses={200: RulesSerializer}
    )
    def get(self, request):
        latest = Rules.objects.order_by('-created_at').first()
        if not latest:
            return Response({"detail": "No rules found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = RulesSerializer(latest)
        return Response(serializer.data)

   

@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistHomeView(APIView):
    """
    Artist Dashboard Home Endpoint.
    Provides income summary, play counts, daily play details, and top songs.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="داشبورد هنرمند",
        description="دریافت آمار کلی درآمد، تعداد پخش‌ها و آهنگ‌های برتر برای صفحه اصلی پنل هنرمند.",
        responses={
            200: inline_serializer(
                name='ArtistHomeResponse',
                fields={
                    'income_summary': inline_serializer(
                        name='IncomeSummary',
                        fields={
                            'today': serializers.DecimalField(max_digits=10, decimal_places=6),
                            'last_7_days': serializers.DecimalField(max_digits=10, decimal_places=6),
                            'last_30_days': serializers.DecimalField(max_digits=10, decimal_places=6),
                            'growth': serializers.DictField(),
                        }
                    ),
                    'plays_summary': inline_serializer(
                        name='PlaysSummary',
                        fields={
                            'today': serializers.IntegerField(),
                            'last_7_days': serializers.IntegerField(),
                            'last_30_days': serializers.IntegerField(),
                            'growth': serializers.DictField(),
                        }
                    ),
                    'daily_plays': serializers.ListField(child=serializers.DictField()),
                    'top_songs': SongSerializer(many=True),
                }
            )
        }
    )
    def get(self, request):
        user = request.user
        # Check if user has artist role
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today_start - timedelta(days=1)
        
        last_7d_start = today_start - timedelta(days=7)
        prev_7d_start = last_7d_start - timedelta(days=7)
        
        last_30d_start = today_start - timedelta(days=30)
        prev_30d_start = last_30d_start - timedelta(days=30)

        def get_stats(start_date, end_date=None):
            qs = PlayCount.objects.filter(songs__artist=artist, created_at__gte=start_date)
            if end_date:
                qs = qs.filter(created_at__lt=end_date)
            
            stats = qs.aggregate(
                total_income=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=10, decimal_places=6))),
                total_plays=Count('id')
            )
            return stats

        def format_growth(current, previous):
            if not previous or previous == 0:
                return None
            growth = ((float(current) - float(previous)) / float(previous)) * 100
            if growth >= 0:
                return f"{growth:.1f}%+"
            else:
                return f"{abs(growth):.1f}%-"

        # Stats
        today_stats = get_stats(today_start)
        yesterday_stats = get_stats(yesterday_start, today_start)
        
        last_7d_stats = get_stats(last_7d_start)
        prev_7d_stats = get_stats(prev_7d_start, last_7d_start)
        
        last_30d_stats = get_stats(last_30d_start)
        prev_30d_stats = get_stats(prev_30d_start, last_30d_start)

        # Income Summary
        income_summary = {
            "today": today_stats['total_income'],
            "last_7_days": last_7d_stats['total_income'],
            "last_30_days": last_30d_stats['total_income'],
            "growth": {
                "today": format_growth(today_stats['total_income'], yesterday_stats['total_income']),
                "last_7_days": format_growth(last_7d_stats['total_income'], prev_7d_stats['total_income']),
                "last_30_days": format_growth(last_30d_stats['total_income'], prev_30d_stats['total_income']),
            }
        }

        # Play Counts Summary
        plays_summary = {
            "today": today_stats['total_plays'],
            "last_7_days": last_7d_stats['total_plays'],
            "last_30_days": last_30d_stats['total_plays'],
            "growth": {
                "today": format_growth(today_stats['total_plays'], yesterday_stats['total_plays']),
                "last_7_days": format_growth(last_7d_stats['total_plays'], prev_7d_stats['total_plays']),
                "last_30_days": format_growth(last_30d_stats['total_plays'], prev_30d_stats['total_plays']),
            }
        }

        # Daily plays for last 7 days (including today)
        daily_plays = []
        for i in range(7):
            day_start = today_start - timedelta(days=i)
            day_end = day_start + timedelta(days=1)
            count = PlayCount.objects.filter(songs__artist=artist, created_at__gte=day_start, created_at__lt=day_end).count()
            daily_plays.append({
                "date": day_start.date().isoformat(),
                "count": count
            })
        daily_plays.reverse()

        # Top 6 songs
        top_songs_qs = Song.objects.filter(artist=artist).annotate(
            total_plays_calc=F('plays') + Count('play_counts')
        ).order_by('-total_plays_calc')[:6]
        
        top_songs = SongSerializer(top_songs_qs, many=True, context={'request': request}).data

        return Response({
            "income_summary": income_summary,
            "plays_summary": plays_summary,
            "daily_plays": daily_plays,
            "top_songs": top_songs
        })


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistLiveListenersView(APIView):
    """
    Retrieve the current live listener count for the authenticated artist.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="تعداد شنوندگان زنده",
        description="دریافت تعداد کاربرانی که در حال حاضر در حال گوش دادن به آهنگ‌های این هنرمند هستند.",
        responses={
            200: inline_serializer(
                name='ArtistLiveListenersResponse',
                fields={
                    'artist_id': serializers.IntegerField(),
                    'artist_name': serializers.CharField(),
                    'live_listeners': serializers.IntegerField(),
                }
            )
        }
    )
    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "artist_id": artist.id,
            "artist_name": artist.name,
            "live_listeners": artist.live_listeners
        })


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistLiveListenersPollView(APIView):
    """
    Long-polling endpoint for live listener updates.
    Blocks until the set of live listeners changes or a timeout occurs.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="بروزرسانی زنده شنوندگان (Long Polling)",
        description="این متد تا زمان تغییر تعداد شنوندگان یا اتمام زمان (۳۰ ثانیه) منتظر می‌ماند.",
        responses={
            200: inline_serializer(
                name='ArtistLiveListenersPollResponse',
                fields={
                    'live_listeners': serializers.IntegerField(),
                    'changed': serializers.BooleanField(),
                }
            )
        }
    )
    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        def get_current_listeners():
            return set(ActivePlayback.objects.filter(
                song__artist=artist,
                expiration_time__gt=timezone.now()
            ).values_list('user_id', flat=True).distinct())

        initial_listeners = get_current_listeners()
        
        # Long polling loop
        timeout = 30  # seconds
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            current_listeners = get_current_listeners()
            if current_listeners != initial_listeners:
                return Response({
                    "live_listeners": len(current_listeners),
                    "changed": True
                })
            time.sleep(3)  # Check every 3 seconds
            
        return Response({
            "live_listeners": len(initial_listeners),
            "changed": False
        })


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistAnalyticsView(APIView):
    """
    Comprehensive Artist Analytics Endpoint.
    Provides summary stats (plays, likes, income, followers), 
    play charts (hourly/daily), city distribution, and top songs.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="تحلیل و آمار هنرمند",
        description="دریافت آمار دقیق پخش‌ها، لایک‌ها، درآمد و توزیع جغرافیایی شنوندگان.",
        parameters=[
            OpenApiParameter("period", OpenApiTypes.STR, description="بازه زمانی: today, 7d, 30d"),
            OpenApiParameter("chart", OpenApiTypes.STR, description="نوع نمودار: hourly, daily")
        ],
        responses={
            200: inline_serializer(
                name='ArtistAnalyticsResponse',
                fields={
                    'summary': serializers.DictField(),
                    'chart': serializers.DictField(),
                    'city_distribution': serializers.ListField(child=serializers.DictField()),
                    'top_songs': serializers.ListField(child=serializers.DictField()),
                }
            )
        }
    )
    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        period = request.query_params.get('period')  # today, 7d, 30d, or None (all-time)
        chart_type = request.query_params.get('chart', 'daily')  # hourly, daily
        
        now = timezone.now()
        start_date = None
        
        if period == 'today':
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if 'chart' not in request.query_params:
                chart_type = 'hourly'
        elif period == '7d':
            start_date = now - timedelta(days=7)
        elif period == '30d':
            start_date = now - timedelta(days=30)

        # 1. Summary Stats
        # Plays
        play_counts_qs = PlayCount.objects.filter(songs__artist=artist)
        if start_date:
            play_counts_qs = play_counts_qs.filter(created_at__gte=start_date)
        
        total_plays_period = play_counts_qs.count()
        if not start_date:
            legacy_plays = Song.objects.filter(artist=artist).aggregate(total=Sum('plays'))['total'] or 0
            total_plays = total_plays_period + legacy_plays
        else:
            total_plays = total_plays_period

        # Likes
        song_likes_qs = SongLike.objects.filter(song__artist=artist)
        if start_date:
            song_likes_qs = song_likes_qs.filter(created_at__gte=start_date)
        total_likes = song_likes_qs.count()

        # Income
        total_income = play_counts_qs.aggregate(
            total=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=10, decimal_places=6)))
        )['total']

        # Followers
        followers_qs = Follow.objects.filter(followed_artist=artist)
        if start_date:
            followers_qs = followers_qs.filter(created_at__gte=start_date)
            total_followers = followers_qs.count() # New followers in period
        else:
            total_followers = followers_qs.count() # Total followers all-time

        summary = {
            "total_plays": total_plays,
            "total_likes": total_likes,
            "total_income": total_income,
            "total_followers": total_followers,
            "period": period or "all-time"
        }

        # 2. Play Chart Data
        chart_data = []
        if chart_type == 'hourly':
            # If period is today, show today's hours. Otherwise last 24 hours.
            c_start = start_date if period == 'today' else now - timedelta(hours=24)
            plays_by_hour = PlayCount.objects.filter(
                songs__artist=artist, 
                created_at__gte=c_start
            ).annotate(hour=TruncHour('created_at')).values('hour').annotate(count=Count('id')).order_by('hour')
            
            for item in plays_by_hour:
                chart_data.append({
                    "time": item['hour'].isoformat(),
                    "count": item['count']
                })
        else:
            # Daily chart
            # If no period, default to last 30 days for chart
            c_start = start_date if start_date else now - timedelta(days=30)
            plays_by_day = PlayCount.objects.filter(
                songs__artist=artist, 
                created_at__gte=c_start
            ).annotate(day=TruncDate('created_at')).values('day').annotate(count=Count('id')).order_by('day')
            
            for item in plays_by_day:
                chart_data.append({
                    "time": item['day'].isoformat(),
                    "count": item['count']
                })

        # 3. City Distribution
        city_dist = play_counts_qs.values('city').annotate(count=Count('id')).order_by('-count')
        city_data = []
        for item in city_dist:
            percentage = (item['count'] / total_plays_period * 100) if total_plays_period > 0 else 0
            city_data.append({
                'city': item['city'] or "Unknown",
                'count': item['count'],
                'percentage': round(percentage, 2)
            })

        # 4. Most Played Songs
        # We'll use the period plays for ranking
        top_songs_qs = Song.objects.filter(artist=artist).annotate(
            period_plays_count=Count('play_counts', filter=Q(play_counts__created_at__gte=start_date) if start_date else Q())
        )
        
        if not start_date:
            top_songs_qs = top_songs_qs.annotate(
                total_plays_calc=F('plays') + F('period_plays_count')
            ).order_by('-total_plays_calc')[:10]
        else:
            top_songs_qs = top_songs_qs.order_by('-period_plays_count')[:10]
            
        top_songs = []
        for s in top_songs_qs:
            top_songs.append({
                "id": s.id,
                "title": s.title,
                "plays": s.total_plays_calc if not start_date else s.period_plays_count,
                "cover_image": s.cover_image
            })

        return Response({
            "summary": summary,
            "chart": {
                "type": chart_type,
                "data": chart_data
            },
            "city_distribution": city_data,
            "top_songs": top_songs
        })


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class DepositRequestView(APIView):
    """
    View for artists to manage their deposit requests.
    Artists can list their requests and submit new ones.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لیست درخواست‌های تسویه هنرمند",
        description="دریافت لیست تمامی درخواست‌های تسویه حساب ثبت شده توسط هنرمند فعلی.",
        responses={200: DepositRequestSerializer(many=True)}
    )
    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        requests = DepositRequest.objects.filter(artist=artist)
        serializer = DepositRequestSerializer(requests, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="ثبت درخواست تسویه جدید",
        description="ثبت درخواست برای دریافت درآمد حاصل از پخش آهنگ‌ها. هنرمند نباید درخواست در حال بررسی داشته باشد.",
        responses={201: DepositRequestSerializer}
    )
    def post(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        # Check if there's already a pending request
        if DepositRequest.objects.filter(artist=artist, status=DepositRequest.STATUS_PENDING).exists():
            return Response({"error": "You already have a pending deposit request"}, status=status.HTTP_400_BAD_REQUEST)

        # Calculate summary of plays to pay
        plays = PlayCount.objects.filter(songs__artist=artist)
        total_plays = plays.count()
        
        if total_plays == 0:
            return Response({"error": "No plays found to request deposit"}, status=status.HTTP_400_BAD_REQUEST)
            
        free_plays = plays.filter(user__plan=User.PLAN_FREE).count()
        premium_plays = plays.filter(user__plan=User.PLAN_PREMIUM).count()
        
        free_percentage = (free_plays / total_plays) * 100 if total_plays > 0 else 0
        premium_percentage = (premium_plays / total_plays) * 100 if total_plays > 0 else 0
        
        summary = {
            "total_plays": total_plays,
            "free_plays": free_plays,
            "premium_plays": premium_plays,
            "free_percentage": f"{free_percentage:.1f}%",
            "premium_percentage": f"{premium_percentage:.1f}%",
            "text": f"{free_percentage:.1f}% free account plays and {premium_percentage:.1f}% paid accounts"
        }
        
        # Calculate total amount to pay (sum of 'pay' field in PlayCount)
        total_amount = plays.aggregate(total=Sum('pay'))['total'] or 0
        
        deposit_request = DepositRequest.objects.create(
            artist=artist,
            amount=total_amount,
            summary=summary
        )
        
        serializer = DepositRequestSerializer(deposit_request)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistWalletView(APIView):
    """
    View to get artist's financial summary:
    - Total Credit: Sum of all 'pay' from PlayCount.
    - Requested Credit: Sum of 'amount' from DepositRequest (Pending, Approved, Done).
    - Available Credit: Total - Requested.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="کیف پول هنرمند",
        description="دریافت موجودی کل، موجودی در حال تسویه و موجودی قابل برداشت هنرمند.",
        responses={
            200: inline_serializer(
                name='ArtistWalletResponse',
                fields={
                    'total_credit': serializers.DecimalField(max_digits=15, decimal_places=6),
                    'requested_credit': serializers.DecimalField(max_digits=15, decimal_places=2),
                    'available_credit': serializers.DecimalField(max_digits=15, decimal_places=6),
                    'deposit_requests': serializers.DictField(),
                }
            )
        }
    )
    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        # 1. Total Credit
        total_credit = PlayCount.objects.filter(songs__artist=artist).aggregate(
            total=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6)))
        )['total']

        # 2. Requested Credit (Pending, Approved, Done)
        requested_credit = DepositRequest.objects.filter(
            artist=artist,
            status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_DONE]
        ).aggregate(
            total=Coalesce(Sum('amount'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=2)))
        )['total']

        # 3. Available Credit
        available_credit = total_credit - requested_credit

        # Deposit request counts breakdown
        requests_qs = DepositRequest.objects.filter(artist=artist)
        total_submissions = requests_qs.count()
        pending_count = requests_qs.filter(status=DepositRequest.STATUS_PENDING).count()
        approved_count = requests_qs.filter(status=DepositRequest.STATUS_APPROVED).count()
        rejected_count = requests_qs.filter(status=DepositRequest.STATUS_REJECTED).count()
        done_count = requests_qs.filter(status=DepositRequest.STATUS_DONE).count()

        return Response({
            "total_credit": total_credit,
            "requested_credit": requested_credit,
            "available_credit": max(0, available_credit),
            "deposit_requests": {
                "total_submissions": total_submissions,
                "pending": pending_count,
                "approved": approved_count,
                "rejected": rejected_count,
                "done": done_count
            }
        })


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistFinanceView(APIView):
    """
    Artist financial overview endpoint.
    - GET /artist/finance/?period=<all|daily|weekly|monthly|today|7d|30d>
    - No param -> all-time

    Returns summary (income amount, percent change, plays) and chart data.
    If period is `all` (no param) chart shows free vs premium totals.
    If period is `daily|weekly|monthly` chart returns one data point per day/week/month.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="آمار مالی هنرمند",
        description="دریافت آمار دقیق درآمد و پخش‌ها با قابلیت فیلتر بر اساس بازه زمانی و نوع نمودار.",
        parameters=[
            OpenApiParameter("period", OpenApiTypes.STR, description="بازه زمانی: all, daily, weekly, monthly, today, 7d, 30d")
        ],
        responses={
            200: inline_serializer(
                name='ArtistFinanceResponse',
                fields={
                    'summary': serializers.DictField(),
                    'chart': serializers.ListField(child=serializers.DictField()),
                }
            )
        }
    )
    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)

        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        period = request.query_params.get('period')  # None=all, 'daily','weekly','monthly','today','7d','30d'
        now = timezone.now()

        # Determine current and previous windows for percent change
        if not period or period == 'all':
            # All time: we compute totals and breakdown by free/premium
            plays_qs = PlayCount.objects.filter(songs__artist=artist)

            total_income = plays_qs.aggregate(total=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))))['total']
            free_income = plays_qs.filter(user__plan=User.PLAN_FREE).aggregate(total=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))))['total']
            premium_income = plays_qs.filter(user__plan=User.PLAN_PREMIUM).aggregate(total=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))))['total']

            total_plays = plays_qs.count()

            # No meaningful previous period for all-time; set change to None
            change_pct = None

            chart = [
                {"label": "free", "amount": free_income},
                {"label": "premium", "amount": premium_income}
            ]

            summary = {
                "income_change_pct": f"{change_pct}" if change_pct is not None else None,
                "income_amount": total_income,
                "currency": "تومان",
                "plays_count": total_plays
            }

            return Response({"summary": summary, "chart": chart})

        # For time-bounded periods: compute start and previous window
        if period == 'today':
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            prev_start = start - timedelta(days=1)
            prev_end = start
            group = 'daily'
        elif period == '7d':
            start = now - timedelta(days=7)
            prev_start = start - timedelta(days=7)
            prev_end = start
            group = 'daily'
        elif period == '30d':
            start = now - timedelta(days=30)
            prev_start = start - timedelta(days=30)
            prev_end = start
            group = 'daily'
        elif period == 'daily':
            # Last 30 days by day
            start = now - timedelta(days=30)
            prev_start = start - timedelta(days=30)
            prev_end = start
            group = 'daily'
        elif period == 'weekly':
            # Last 12 weeks
            start = now - timedelta(weeks=12)
            prev_start = start - timedelta(weeks=12)
            prev_end = start
            group = 'weekly'
        elif period == 'monthly':
            # Last 12 months
            start = now - timedelta(days=365)
            prev_start = start - timedelta(days=365)
            prev_end = start
            group = 'monthly'
        else:
            # Fallback: treat as last 30 days
            start = now - timedelta(days=30)
            prev_start = start - timedelta(days=30)
            prev_end = start
            group = 'daily'

        # Current and previous sums
        current_qs = PlayCount.objects.filter(songs__artist=artist, created_at__gte=start)
        prev_qs = PlayCount.objects.filter(songs__artist=artist, created_at__gte=prev_start, created_at__lt=prev_end)

        current_income = current_qs.aggregate(total=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))))['total']
        prev_income = prev_qs.aggregate(total=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))))['total']

        # percent change
        def pct_change(current, previous):
            try:
                if previous in (None, 0):
                    return None
                return round(((float(current) - float(previous)) / float(previous)) * 100, 1)
            except Exception:
                return None

        change_pct = pct_change(current_income, prev_income)

        total_plays = current_qs.count()

        # Build chart grouped by requested granularity
        chart = []
        if group == 'daily':
            rows = current_qs.annotate(period=TruncDate('created_at')).values('period').annotate(
                income=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))),
                free_income=Coalesce(Sum('pay', filter=Q(user__plan=User.PLAN_FREE)), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))),
                premium_income=Coalesce(Sum('pay', filter=Q(user__plan=User.PLAN_PREMIUM)), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))),
                plays=Count('id')
            ).order_by('period')

            for r in rows:
                chart.append({
                    'time': r['period'].isoformat(),
                    'income': r['income'],
                    'free_income': r['free_income'],
                    'premium_income': r['premium_income'],
                    'plays': r['plays']
                })

        elif group == 'weekly':
            rows = current_qs.annotate(period=TruncWeek('created_at')).values('period').annotate(
                income=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))),
                free_income=Coalesce(Sum('pay', filter=Q(user__plan=User.PLAN_FREE)), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))),
                premium_income=Coalesce(Sum('pay', filter=Q(user__plan=User.PLAN_PREMIUM)), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))),
                plays=Count('id')
            ).order_by('period')

            for r in rows:
                chart.append({
                    'time': r['period'].isoformat(),
                    'income': r['income'],
                    'free_income': r['free_income'],
                    'premium_income': r['premium_income'],
                    'plays': r['plays']
                })

        else:  # monthly
            rows = current_qs.annotate(period=TruncMonth('created_at')).values('period').annotate(
                income=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))),
                free_income=Coalesce(Sum('pay', filter=Q(user__plan=User.PLAN_FREE)), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))),
                premium_income=Coalesce(Sum('pay', filter=Q(user__plan=User.PLAN_PREMIUM)), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6))),
                plays=Count('id')
            ).order_by('period')

            for r in rows:
                chart.append({
                    'time': r['period'].isoformat(),
                    'income': r['income'],
                    'free_income': r['free_income'],
                    'premium_income': r['premium_income'],
                    'plays': r['plays']
                })

        summary = {
            'income_change_pct': f"{change_pct}%" if change_pct is not None else None,
            'income_amount': current_income,
            'currency': 'تومان',
            'plays_count': total_plays,
            'period': period
        }

        return Response({
            'summary': summary,
            'chart': chart
        })


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistFinanceSongsView(APIView):
    """
    Return paginated list of artist's songs with total income and plays.
    - default sort: most income (desc)
    - query param `sort=release_date` will sort by release_date (desc)
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="آمار مالی آهنگ‌های هنرمند",
        description="دریافت لیست آهنگ‌های هنرمند به همراه درآمد و تعداد پخش هر کدام با قابلیت مرتب‌سازی.",
        parameters=[
            OpenApiParameter("sort", OpenApiTypes.STR, description="مرتب‌سازی: release_date یا income (پیش‌فرض)")
        ],
        responses={200: SongSerializer(many=True)}
    )
    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)

        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        sort = request.query_params.get('sort')

        # Annotate songs with income and play counts
        qs = Song.objects.filter(artist=artist).annotate(
            play_counts_count=Count('play_counts'),
            income=Coalesce(Sum('play_counts__pay'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=6)))
        ).annotate(
            total_plays=F('plays') + F('play_counts_count')
        )

        # Sorting
        if sort == 'release_date':
            qs = qs.order_by('-release_date', '-income')
        else:
            # default: sort by income desc, tie-breaker total_plays desc
            qs = qs.order_by('-income', '-total_plays')

        # Pagination
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(qs, request)
        if page is not None:
            serializer = SongSerializer(page, many=True, context={'request': request})
            results = []
            for song_obj, song_data in zip(page, serializer.data):
                results.append({
                    **song_data,
                    'income': getattr(song_obj, 'income', 0),
                    'total_plays': int(getattr(song_obj, 'total_plays', 0))
                })
            return paginator.get_paginated_response(results)

        # non-paginated fallback
        serializer = SongSerializer(qs, many=True, context={'request': request})
        results = []
        for song_obj, song_data in zip(qs, serializer.data):
            results.append({
                **song_data,
                'income': getattr(song_obj, 'income', 0),
                'total_plays': int(getattr(song_obj, 'total_plays', 0))
            })
        return Response(results)


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistSettingsView(APIView):
    """Allow an artist to update their profile information and photos.
    Supports PUT (full replace) and PATCH (partial update).
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get_artist(self, user):
        if User.ROLE_ARTIST not in user.roles:
            return None
        try:
            return user.artist_profile
        except Artist.DoesNotExist:
            return None

    @extend_schema(
        summary="به‌روزرسانی کامل پروفایل هنرمند",
        description="به‌روزرسانی تمامی اطلاعات پروفایل هنرمند شامل نام، بیوگرافی، تصاویر و اطلاعات هویتی.",
        request={
            'multipart/form-data': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'artistic_name': {'type': 'string'},
                    'bio': {'type': 'string'},
                    'profile_image': {'type': 'string', 'format': 'binary'},
                    'banner_image': {'type': 'string', 'format': 'binary'},
                    'email': {'type': 'string'},
                    'city': {'type': 'string'},
                    'date_of_birth': {'type': 'string', 'format': 'date'},
                    'address': {'type': 'string'},
                    'id_number': {'type': 'string'},
                }
            }
        },
        responses={200: ArtistSerializer}
    )
    def put(self, request):
        return self._update(request, partial=False)

    @extend_schema(
        summary="به‌روزرسانی جزئی پروفایل هنرمند",
        description="به‌روزرسانی برخی از فیلدهای پروفایل هنرمند.",
        request={
            'multipart/form-data': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'artistic_name': {'type': 'string'},
                    'bio': {'type': 'string'},
                    'profile_image': {'type': 'string', 'format': 'binary'},
                    'banner_image': {'type': 'string', 'format': 'binary'},
                }
            }
        },
        responses={200: ArtistSerializer}
    )
    def patch(self, request):
        return self._update(request, partial=True)

    def _update(self, request, partial=True):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        data = request.data.copy()

        # Handle images (upload to R2 and store URL)
        profile_file = request.FILES.get('profile_image')
        if profile_file:
            try:
                url, _ = upload_file_to_r2(profile_file, folder='artists', custom_filename=None)
                artist.profile_image = url
            except Exception as e:
                return Response({"error": f"Profile image upload failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        banner_file = request.FILES.get('banner_image')
        if banner_file:
            try:
                url, _ = upload_file_to_r2(banner_file, folder='artists', custom_filename=None)
                artist.banner_image = url
            except Exception as e:
                return Response({"error": f"Banner image upload failed: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Updatable fields
        updatable = ['name', 'artistic_name', 'email', 'city', 'date_of_birth', 'address', 'id_number', 'bio']
        for f in updatable:
            if f in data:
                val = data.get(f)
                # date_of_birth may come as empty string; handle null
                if f == 'date_of_birth' and val in (None, '', 'null'):
                    setattr(artist, f, None)
                else:
                    setattr(artist, f, val)

        try:
            artist.save()
        except Exception as e:
            return Response({"error": f"Failed to save artist: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ArtistSerializer(artist, context={'request': request})
        return Response(serializer.data)


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistChangePasswordView(APIView):
    """Change user's account password using current password and new password."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="تغییر رمز عبور هنرمند",
        description="تغییر رمز عبور حساب کاربری هنرمند با استفاده از رمز عبور فعلی و رمز عبور جدید.",
        request=inline_serializer(
            name='ArtistChangePasswordRequest',
            fields={
                'current_password': serializers.CharField(),
                'new_password': serializers.CharField(),
            }
        ),
        responses={
            200: inline_serializer(
                name='ArtistChangePasswordResponse',
                fields={
                    'message': serializers.CharField()
                }
            )
        }
    )
    def post(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)

        current = request.data.get('current_password')
        new = request.data.get('new_password')

        if not current or not new:
            return Response({"error": "Both 'current_password' and 'new_password' are required."}, status=status.HTTP_400_BAD_REQUEST)

        if not user.check_password(current):
            return Response({"error": "Current password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)

        # Basic validation for new password length
        if len(new) < 6:
            return Response({"error": "New password must be at least 6 characters long."}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new)
        user.save()

        return Response({"status": "password_changed"}, status=status.HTTP_200_OK)


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistSongsManagementView(APIView):
    """
    View for artists to manage their own songs.
    Supports listing, uploading, and updating songs.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get_artist(self, user):
        if User.ROLE_ARTIST not in user.roles:
            return None
        try:
            return user.artist_profile
        except Artist.DoesNotExist:
            return None

    @extend_schema(
        summary="لیست یا جزئیات آهنگ‌های هنرمند",
        description="دریافت لیست تمامی آهنگ‌های هنرمند یا جزئیات و آمار یک آهنگ خاص.",
        parameters=[
            OpenApiParameter("days", OpenApiTypes.INT, description="تعداد روزها برای آمار (پیش‌فرض ۳۰)"),
            OpenApiParameter("status", OpenApiTypes.STR, description="فیلتر بر اساس وضعیت (pending, approved, rejected)")
        ],
        responses={200: SongSerializer(many=True)}
    )
    def get(self, request, pk=None):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        if pk:
            song = get_object_or_404(Song, pk=pk, artist=artist)
            
            # Analytics parameters
            try:
                days = int(request.query_params.get('days', 30))
            except (ValueError, TypeError):
                days = 30
            
            start_date = timezone.now() - timedelta(days=days)
            
            # Total stats
            total_plays = (song.plays or 0) + song.play_counts.count()
            total_likes = song.liked_by.count()
            added_to_playlists = song.user_playlists.count()
            
            # Analytics for the period
            period_plays = song.play_counts.filter(created_at__gte=start_date)
            total_period_plays = period_plays.count()
            
            # Daily plays for chart
            daily_plays = period_plays.annotate(date=TruncDate('created_at')) \
                .values('date').annotate(count=Count('id')).order_by('date')
            
            # City distribution
            city_dist = period_plays.values('city').annotate(count=Count('id')).order_by('-count')
            city_data = []
            for item in city_dist:
                percentage = (item['count'] / total_period_plays * 100) if total_period_plays > 0 else 0
                city_data.append({
                    'city': item['city'],
                    'count': item['count'],
                    'percentage': round(percentage, 2)
                })
                
            # Country distribution
            country_dist = period_plays.values('country').annotate(count=Count('id')).order_by('-count')
            country_data = []
            for item in country_dist:
                percentage = (item['count'] / total_period_plays * 100) if total_period_plays > 0 else 0
                country_data.append({
                    'country': item['country'],
                    'count': item['count'],
                    'percentage': round(percentage, 2)
                })
                
            serializer = SongSerializer(song, context={'request': request})
            data = serializer.data
            data['analytics'] = {
                'days': days,
                'total_period_plays': total_period_plays,
                'daily_plays': list(daily_plays),
                'city_distribution': city_data,
                'country_distribution': country_data
            }
            return Response(data)

        queryset = Song.objects.filter(artist=artist).order_by('-release_date', '-created_at')
        
        status_param = request.query_params.get('status')
        if status_param:
            queryset = queryset.filter(status=status_param)

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(queryset, request)
        if page is not None:
            serializer = SongSerializer(page, many=True, context={'request': request})
            return paginator.get_paginated_response(serializer.data)

        serializer = SongSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="آپلود آهنگ جدید",
        description="آپلود فایل صوتی و کاور آهنگ جدید به همراه اطلاعات متادیتا.",
        request={
            'multipart/form-data': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string'},
                    'audio_file': {'type': 'string', 'format': 'binary'},
                    'cover_image': {'type': 'string', 'format': 'binary'},
                    'genre_ids': {'type': 'array', 'items': {'type': 'integer'}},
                    'mood_ids': {'type': 'array', 'items': {'type': 'integer'}},
                    'tag_ids': {'type': 'array', 'items': {'type': 'integer'}},
                }
            }
        },
        responses={201: SongSerializer}
    )
    def post(self, request):
        print(f"DEBUG: ArtistSongsManagementView.post started for user {request.user}")
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        audio_file = request.FILES.get('audio_file')
        if not audio_file:
            return Response({"error": "audio_file is required"}, status=status.HTTP_400_BAD_REQUEST)

        title = request.data.get('title', 'Untitled')
        
        # Determine artist name for filename
        artist_name = artist.artistic_name or artist.name
        featured = request.data.getlist('featured_artists') if hasattr(request.data, 'getlist') else request.data.get('featured_artists', [])

        # Get audio info
        duration, bitrate, format_ext = get_audio_info(audio_file)
        if not format_ext:
            # Fallback to extension
            _, ext = os.path.splitext(audio_file.name)
            format_ext = ext.lstrip('.').lower()
        
        # Build filename base and sanitize
        if featured:
            filename_base = f"{artist_name} - {title} (feat. {', '.join(featured)})"
        else:
            filename_base = f"{artist_name} - {title}"
        safe_filename_base = make_safe_filename(filename_base)
        audio_filename = f"{safe_filename_base}.{format_ext}"
        
        # Upload original
        print(f"DEBUG: Uploading original file: {audio_filename}")
        audio_url, _ = upload_file_to_r2(audio_file, folder='songs', custom_filename=audio_filename)
        print(f"DEBUG: Original file uploaded to: {audio_url}")
        
        converted_url = None
        # Convert if it's not mp3 OR if it's mp3 with bitrate > 128 OR if bitrate is unknown
        print(f"DEBUG: format_ext={format_ext}, bitrate={bitrate}")
        if format_ext != 'mp3' or bitrate is None or bitrate > 128:
            print(f"DEBUG: Starting conversion to 128kbps...")
            try:
                if hasattr(audio_file, 'seek'):
                    audio_file.seek(0)
                converted_file = convert_to_128kbps(audio_file)
                conv_filename = f"{safe_filename_base}_128.mp3"
                print(f"DEBUG: Uploading converted file: {conv_filename}")
                converted_url, _ = upload_file_to_r2(converted_file, folder='songs/128', custom_filename=conv_filename)
                print(f"DEBUG: Converted file uploaded to: {converted_url}")
            except Exception as e:
                print(f"DEBUG: Conversion failed: {str(e)}")
                import traceback
                traceback.print_exc()

        # Handle cover image
        cover_image = request.FILES.get('cover_image')
        cover_url = ""
        if cover_image:
            _, ext = os.path.splitext(cover_image.name)
            cover_filename = f"{safe_filename_base}_cover{ext}"
            cover_url, _ = upload_file_to_r2(cover_image, folder='covers', custom_filename=cover_filename)

        # Create a clean dict (avoid copying request.data which may include file objects)
        clean = {}

        # Copy simple scalar fields if provided
        scalar_fields = ['title', 'is_single', 'release_date', 'language', 'description', 'lyrics',
                         'tempo', 'energy', 'danceability', 'valence', 'acousticness', 'instrumentalness',
                         'speechiness', 'live_performed', 'label', 'credits']
        for field in scalar_fields:
            if field in request.data:
                clean[field] = request.data.get(field)

        # Copy list fields (producers, composers, lyricists)
        for list_field in ['producers', 'composers', 'lyricists']:
            if hasattr(request.data, 'getlist'):
                val = request.data.getlist(list_field)
            else:
                val = request.data.get(list_field)
            if val:
                clean[list_field] = val

        # Map many-to-many id lists to serializer write-only fields
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids']:
            if hasattr(request.data, 'getlist'):
                val = request.data.getlist(field)
            else:
                val = request.data.get(field)
            if val:
                clean[f"{field}_write"] = val

        # Attach the derived fields (strings/ids only)
        clean['artist'] = artist.id
        clean['audio_file'] = audio_url
        if converted_url:
            clean['converted_audio_url'] = converted_url
        clean['cover_image'] = cover_url
        if duration is not None:
            clean['duration_seconds'] = duration
        clean['original_format'] = format_ext
        clean['uploader'] = request.user.id

        print(f"DEBUG: Final clean data for serializer: {clean}")

        serializer = SongSerializer(data=clean, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            print(f"DEBUG: Song saved successfully. ID: {serializer.instance.id}")
            return Response({
                "message": "OK",
                "song": serializer.data
            }, status=status.HTTP_201_CREATED)
        print(f"DEBUG: Serializer errors: {serializer.errors}")
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="به‌روزرسانی کامل آهنگ",
        description="به‌روزرسانی تمامی اطلاعات یک آهنگ خاص.",
        responses={200: SongSerializer}
    )
    def put(self, request, pk=None):
        return self.update(request, pk, partial=False)

    @extend_schema(
        summary="به‌روزرسانی جزئی آهنگ",
        description="به‌روزرسانی برخی از فیلدهای یک آهنگ خاص.",
        responses={200: SongSerializer}
    )
    def patch(self, request, pk=None):
        return self.update(request, pk, partial=True)

    def update(self, request, pk, partial=False):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        song = get_object_or_404(Song, pk=pk, artist=artist)
        
        data = request.data.copy()

        # Map user-friendly field names to serializer write_only fields
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids']:
            if field in data and f"{field}_write" not in data:
                data[f"{field}_write"] = data.getlist(field) if hasattr(data, 'getlist') else data[field]
        
        audio_file = request.FILES.get('audio_file')
        if audio_file:
            title = data.get('title', song.title)
            artist_name = artist.artistic_name or artist.name
            featured = data.getlist('featured_artists') if hasattr(data, 'getlist') else data.get('featured_artists', song.featured_artists)
            
            duration, bitrate, format_ext = get_audio_info(audio_file)
            if not format_ext:
                _, ext = os.path.splitext(audio_file.name)
                format_ext = ext.lstrip('.').lower()
            
            # Build filename base and sanitize
            if featured:
                filename_base = f"{artist_name} - {title} (feat. {', '.join(featured)})"
            else:
                filename_base = f"{artist_name} - {title}"
            safe_filename_base = make_safe_filename(filename_base)
            audio_filename = f"{safe_filename_base}.{format_ext}"
            
            audio_url, _ = upload_file_to_r2(audio_file, folder='songs', custom_filename=audio_filename)
            data['audio_file'] = audio_url
            data['duration_seconds'] = duration
            data['original_format'] = format_ext
            
            if format_ext != 'mp3' or bitrate is None or bitrate > 128:
                try:
                    if hasattr(audio_file, 'seek'):
                        audio_file.seek(0)
                    converted_file = convert_to_128kbps(audio_file)
                    conv_filename = f"{safe_filename_base}_128.mp3"
                    converted_url, _ = upload_file_to_r2(converted_file, folder='songs/128', custom_filename=conv_filename)
                    data['converted_audio_url'] = converted_url
                except Exception as e:
                    print(f"Conversion failed: {e}")

        cover_image = request.FILES.get('cover_image')
        if cover_image:
            title = data.get('title', song.title)
            artist_name = artist.artistic_name or artist.name
            featured = data.getlist('featured_artists') if hasattr(data, 'getlist') else data.get('featured_artists', song.featured_artists)
            
            if featured:
                filename_base = f"{artist_name} - {title} (feat. {', '.join(featured)})"
            else:
                filename_base = f"{artist_name} - {title}"
            
            safe_filename_base = make_safe_filename(filename_base)
            _, ext = os.path.splitext(cover_image.name)
            cover_filename = f"{safe_filename_base}_cover{ext}"
            cover_url, _ = upload_file_to_r2(cover_image, folder='covers', custom_filename=cover_filename)
            data['cover_image'] = cover_url

        serializer = SongSerializer(song, data=data, partial=partial, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response({
                "message": "OK",
                "song": serializer.data
            })
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistAlbumsManagementView(APIView):
    """
    View for artists to manage their own albums.
    Supports listing, creating (with multiple songs), and updating albums.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get_artist(self, user):
        if User.ROLE_ARTIST not in user.roles:
            return None
        try:
            return user.artist_profile
        except Artist.DoesNotExist:
            return None

    @extend_schema(
        summary="لیست یا جزئیات آلبوم‌های هنرمند",
        description="دریافت لیست تمامی آلبوم‌های هنرمند یا جزئیات یک آلبوم خاص به همراه آهنگ‌های آن.",
        responses={200: AlbumSerializer(many=True)}
    )
    def get(self, request, pk=None):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        if pk:
            album = get_object_or_404(Album, pk=pk, artist=artist)
            serializer = AlbumSerializer(album, context={'request': request})
            data = serializer.data
            # Include songs in detail view
            songs_qs = Song.objects.filter(album=album).order_by('id')
            data['songs'] = SongSerializer(songs_qs, many=True, context={'request': request}).data
            return Response(data)

        queryset = Album.objects.filter(artist=artist).order_by('-release_date', '-created_at')
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(queryset, request)
        if page is not None:
            serializer = AlbumSerializer(page, many=True, context={'request': request})
            return paginator.get_paginated_response(serializer.data)

        serializer = AlbumSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ایجاد آلبوم جدید",
        description="ایجاد آلبوم جدید به همراه آپلود همزمان چندین آهنگ. آهنگ‌ها می‌توانند جدید باشند یا از آهنگ‌های موجود انتخاب شوند.",
        request={
            'multipart/form-data': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string'},
                    'cover_image': {'type': 'string', 'format': 'binary'},
                    'release_date': {'type': 'string', 'format': 'date'},
                    'existing_song_ids': {'type': 'array', 'items': {'type': 'integer'}},
                    # Dynamic song fields: song1-title, song1-audio_file, etc.
                }
            }
        },
        responses={201: AlbumSerializer}
    )
    def post(self, request):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        # 1. Create Album
        album_data = request.data.copy()
        
        # Map user-friendly field names to serializer write_only fields for album
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids']:
            if field in album_data and f"{field}_write" not in album_data:
                album_data[f"{field}_write"] = album_data.getlist(field) if hasattr(album_data, 'getlist') else album_data[field]

        # Handle album cover
        album_cover = request.FILES.get('cover_image')
        if album_cover:
            safe_title = "".join([c for c in album_data.get('title', 'album') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            safe_artist = "".join([c for c in (artist.artistic_name or artist.name) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            cover_filename = f"{safe_artist} - {safe_title}_album_cover"
            cover_url, _ = upload_file_to_r2(album_cover, folder='covers', custom_filename=cover_filename)
            album_data['cover_image'] = cover_url

        album_data['artist'] = artist.id
        
        album_serializer = AlbumSerializer(data=album_data, context={'request': request})
        if not album_serializer.is_valid():
            return Response(album_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        album = album_serializer.save()

        # 2. Process Songs
        # Handle existing songs
        existing_song_ids = request.data.getlist('existing_song_ids')
        if existing_song_ids:
            Song.objects.filter(id__in=existing_song_ids, artist=artist).update(album=album)

        # Process new songs
        song_index = 1
        created_songs = []
        while True:
            prefix = f"song{song_index}-"
            title = request.data.get(f"{prefix}title")
            audio_file = request.FILES.get(f"{prefix}audio_file")
            
            # If we don't find title or audio, we might have reached the end
            if not title and not audio_file:
                if song_index > 50: # Reasonable limit
                    break
                song_index += 1
                continue
            
            if not audio_file:
                song_index += 1
                continue

            # Process this song
            artist_name = artist.artistic_name or artist.name
            duration, bitrate, format_ext = get_audio_info(audio_file)
            if not format_ext:
                _, ext = os.path.splitext(audio_file.name)
                format_ext = ext.lstrip('.').lower()
            
            # Build filename base
            # Note: featured artists for individual songs in album creation might not be supported in the current form structure,
            # but we'll use the artist name and title.
            filename_base = f"{artist_name} - {title}"
            safe_filename_base = make_safe_filename(filename_base)
            audio_filename = f"{safe_filename_base}.{format_ext}"
            
            audio_url, _ = upload_file_to_r2(audio_file, folder='songs', custom_filename=audio_filename)
            
            converted_url = None
            if format_ext != 'mp3' or bitrate is None or bitrate > 128:
                try:
                    if hasattr(audio_file, 'seek'):
                        audio_file.seek(0)
                    converted_file = convert_to_128kbps(audio_file)
                    conv_filename = f"{safe_filename_base}_128.mp3"
                    converted_url, _ = upload_file_to_r2(converted_file, folder='songs/128', custom_filename=conv_filename)
                except Exception:
                    pass

            song_cover = request.FILES.get(f"{prefix}cover_image")
            song_cover_url = ""
            if song_cover:
                _, ext = os.path.splitext(song_cover.name)
                cover_filename = f"{safe_filename_base}_cover{ext}"
                song_cover_url, _ = upload_file_to_r2(song_cover, folder='covers', custom_filename=cover_filename)
            else:
                song_cover_url = album.cover_image

            # Prepare song data for serializer
            song_data = {
                'title': title,
                'artist': artist.id,
                'album': album.id,
                'audio_file': audio_url,
                'converted_audio_url': converted_url,
                'cover_image': song_cover_url,
                'duration_seconds': duration,
                'original_format': format_ext,
                'uploader': request.user.id,
                'status': Song.STATUS_PUBLISHED,
                'lyrics': request.data.get(f"{prefix}lyrics", ""),
                'description': request.data.get(f"{prefix}description", ""),
                'release_date': album.release_date,
                'language': request.data.get(f"{prefix}language", "fa"),
            }
            
            # Handle JSON fields
            for list_field in ['featured_artists', 'producers', 'composers', 'lyricists']:
                val = request.data.getlist(f"{prefix}{list_field}")
                if val:
                    song_data[list_field] = val

            # Handle ManyToMany IDs
            for id_field in ['genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids']:
                val = request.data.getlist(f"{prefix}{id_field}")
                if val:
                    song_data[f"{id_field}_write"] = val

            song_serializer = SongSerializer(data=song_data, context={'request': request})
            if song_serializer.is_valid():
                song_serializer.save()
                created_songs.append(song_serializer.data)
            
            song_index += 1

        return Response({
            "message": "Album created successfully",
            "album": album_serializer.data,
            "new_songs": created_songs
        }, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="به‌روزرسانی کامل آلبوم",
        description="به‌روزرسانی تمامی اطلاعات یک آلبوم خاص.",
        responses={200: AlbumSerializer}
    )
    def put(self, request, pk=None):
        return self.update(request, pk, partial=False)

    @extend_schema(
        summary="به‌روزرسانی جزئی آلبوم",
        description="به‌روزرسانی برخی از فیلدهای یک آلبوم خاص.",
        responses={200: AlbumSerializer}
    )
    def patch(self, request, pk=None):
        return self.update(request, pk, partial=True)

    def update(self, request, pk, partial=False):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        album = get_object_or_404(Album, pk=pk, artist=artist)
        
        album_data = request.data.copy()
        
        # Map user-friendly field names to serializer write_only fields for album
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids']:
            if field in album_data and f"{field}_write" not in album_data:
                album_data[f"{field}_write"] = album_data.getlist(field) if hasattr(album_data, 'getlist') else album_data[field]

        album_cover = request.FILES.get('cover_image')
        if album_cover:
            safe_title = "".join([c for c in album_data.get('title', album.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            safe_artist = "".join([c for c in (artist.artistic_name or artist.name) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            cover_filename = f"{safe_artist} - {safe_title}_album_cover"
            cover_url, _ = upload_file_to_r2(album_cover, folder='covers', custom_filename=cover_filename)
            album_data['cover_image'] = cover_url

        serializer = AlbumSerializer(album, data=album_data, partial=partial, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response({
                "message": "Album updated successfully",
                "album": serializer.data
            })
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف آلبوم",
        description="حذف یک آلبوم خاص متعلق به هنرمند.",
        responses={204: None}
    )
    def delete(self, request, pk):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        album = get_object_or_404(Album, pk=pk, artist=artist)
        album.delete()
        return Response({"message": "Album deleted successfully"}, status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و  صفحات جزئیات و عملیات'])
class ReportCreateView(generics.CreateAPIView):
    """Endpoint for users to submit reports for songs or artists."""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = ReportSerializer

    @extend_schema(
        summary="ثبت گزارش تخلف",
        description="ثبت گزارش تخلف برای یک آهنگ یا هنرمند توسط کاربر.",
        responses={201: ReportSerializer}
    )
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و  صفحات جزئیات و عملیات'])
class NotificationListView(generics.ListAPIView):
    """List notifications for the authenticated user or their artist profile."""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = NotificationSerializer

    @extend_schema(
        summary="لیست اعلان‌ها",
        description="دریافت لیست اعلان‌های کاربر یا هنرمند با قابلیت گروه‌بندی هوشمند.",
        parameters=[
            OpenApiParameter("artist", OpenApiTypes.BOOL, description="دریافت اعلان‌های مربوط به پنل هنرمند")
        ],
        responses={200: NotificationSerializer(many=True)}
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    def get_queryset(self):
        user = self.request.user
        is_artist = self.request.query_params.get('artist', '').lower() == 'true'
        
        if is_artist:
            if hasattr(user, 'artist_profile'):
                return Notification.objects.filter(artist=user.artist_profile).order_by('-created_at')
            return Notification.objects.none()
        
        return Notification.objects.filter(user=user).order_by('-created_at')

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        notifications = list(queryset)
        
        if not notifications:
            return self.get_paginated_response([])

        # Grouping logic
        grouped = {} # (template, has_read) -> {sum, obj, uses_farsi, template}
        
        FARSI_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
        ENG_DIGITS = "0123456789"
        farsi_to_eng = str.maketrans(FARSI_DIGITS, ENG_DIGITS)
        eng_to_farsi = str.maketrans(ENG_DIGITS, FARSI_DIGITS)

        for n in notifications:
            text = n.text
            has_read = n.has_read
            
            # Detect Farsi digits
            uses_farsi = any(c in FARSI_DIGITS for c in text)
            
            # Normalize to English digits for extraction
            norm_text = text.translate(farsi_to_eng)
            
            # Find all numbers
            numbers = re.findall(r'\d+', norm_text)
            
            # We only group if there is exactly one number (the "value" the user mentioned)
            if len(numbers) != 1:
                # No numbers or multiple numbers: group by exact text
                key = (text, has_read)
                if key not in grouped:
                    grouped[key] = {'sum': None, 'obj': n, 'is_numeric': False}
                continue
            
            # Template: replace the single number with a placeholder
            template = re.sub(r'\d+', '{}', norm_text)
            key = (template, has_read)
            val = int(numbers[0])
            
            if key not in grouped:
                grouped[key] = {
                    'sum': val,
                    'obj': n,
                    'is_numeric': True,
                    'uses_farsi': uses_farsi,
                    'template': template
                }
            else:
                grouped[key]['sum'] += val
                # Keep the latest object for metadata (id, created_at)
                if n.created_at > grouped[key]['obj'].created_at:
                    grouped[key]['obj'] = n

        # Reconstruct grouped notifications
        result = []
        for data in grouped.values():
            obj = data['obj']
            if data['is_numeric']:
                final_val = str(data['sum'])
                if data['uses_farsi']:
                    final_val = final_val.translate(eng_to_farsi)
                
                # Reconstruct text using the template
                # We use the original language (Farsi/English) based on detection
                text_template = data['template']
                if data['uses_farsi']:
                    # If it was Farsi, the template (from norm_text) is in English, 
                    # but we want to return Farsi text.
                    # Actually, norm_text only changed digits. 
                    # So we translate the template back to Farsi digits if needed.
                    text_template = text_template.translate(eng_to_farsi)
                
                obj.text = text_template.format(final_val)
            
            result.append(obj)
            
        # Sort by created_at desc
        result.sort(key=lambda x: x.created_at, reverse=True)
        
        # Apply pagination to the grouped list
        page = self.paginate_queryset(result)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(result, many=True)
        return Response(serializer.data)


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و  صفحات جزئیات و عملیات'])
class NotificationMarkReadView(APIView):
    """Mark a specific notification or all notifications as read."""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="خوانده شده کردن اعلان‌ها",
        description="علامت‌گذاری یک اعلان خاص یا تمامی اعلان‌ها به عنوان خوانده شده.",
        parameters=[
            OpenApiParameter("artist", OpenApiTypes.BOOL, description="اعمال بر روی اعلان‌های پنل هنرمند")
        ],
        responses={
            200: inline_serializer(
                name='NotificationMarkReadResponse',
                fields={
                    'message': serializers.CharField()
                }
            )
        }
    )
    def post(self, request, pk=None):
        user = request.user
        is_artist = request.query_params.get('artist', '').lower() == 'true'
        
        if pk:
            # Mark specific notification as read
            notification = get_object_or_404(Notification, pk=pk)
            # Security check: ensure notification belongs to the user or their artist profile
            if notification.user == user or (is_artist and hasattr(user, 'artist_profile') and notification.artist == user.artist_profile):
                notification.has_read = True
                notification.save()
                return Response({"message": "Notification marked as read"})
            return Response({"error": "Not authorized"}, status=status.HTTP_403_FORBIDDEN)
        
        # Mark all as read
        if is_artist:
            if hasattr(user, 'artist_profile'):
                Notification.objects.filter(artist=user.artist_profile, has_read=False).update(has_read=True)
            else:
                return Response({"error": "No artist profile found"}, status=status.HTTP_400_BAD_REQUEST)
        else:
            Notification.objects.filter(user=user, has_read=False).update(has_read=True)
            
        return Response({"message": "All notifications marked as read"})


