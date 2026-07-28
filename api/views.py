from rest_framework import generics, permissions, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import action
import re
import uuid
from .models import (
    User, Artist, Album, Playlist,NotificationSetting, Genre, Mood, Tag, SubGenre, Song, 
    StreamAccess, PlayCount, UserPlaylist, RecommendedPlaylist, EventPlaylist, SearchSection,
    ArtistMonthlyListener, UserHistory, Follow, SongLike, AlbumLike, PlaylistLike, Rules, PlayConfiguration,
    ActivePlayback, DepositRequest, Report, Notification, AudioAd, ArtistSocialAccount, DownloadHistory,
    InitialCheck, UserImageProfile
)
from .models import BannerAd, BannerAdServeCounter
from .localization import get_request_language
from .serializers import (
    UserSerializer,PlaylistSerializer,NotificationSettingSerializer,
    RegisterSerializer, 
    ArtistSocialAccountSerializer,
    CustomTokenObtainPairSerializer,
    ArtistSerializer,
    PopularArtistSerializer,
    BannerAdSerializer,
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
    SimplePlaylistSerializer,
    ArtistSocialAccountSerializer,
    UserHistorySerializer,
    UserPublicProfileSerializer,
    UserSearchSummarySerializer,
    DownloadHistorySerializer,
    InitialCheckSerializer,
    UserImageProfileSerializer,
)
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.pagination import PageNumberPagination
from django.db.models import (
    Sum, Count, F, IntegerField, BigIntegerField, Value, Prefetch, DecimalField, CharField,
    TextField, OuterRef, Subquery,
)
from django.db.models.functions import Coalesce, TruncDate, TruncHour, TruncWeek, TruncMonth, Replace, Cast
from django.utils import timezone
from django.conf import settings
from .utils import (
    absolute_api_url, upload_file_to_r2, generate_signed_r2_url,
    get_audio_info, convert_to_128kbps,
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
from django.db.models import Q, Count, Avg, F, Value
from django.db.models.functions import Concat, Replace, Lower
from django.shortcuts import get_object_or_404
from django.urls import reverse
from rest_framework import serializers
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, inline_serializer
from .performance import (
    AFFINITY_VERSION_KEY, CATALOG_VERSION_KEY, USER_DIRECTORY_VERSION_KEY,
    cache_delete, cache_get, cache_get_or_claim, cache_set,
    cache_version, hydrate_album_metrics, hydrate_artist_metrics, hydrate_playlist_metrics,
    hydrate_song_metrics, stable_cache_key,
)
from collections import Counter
import json



class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

    def _page_link(self, number):
        params = self.request.query_params.copy()
        params[self.page_query_param] = number
        query = params.urlencode()
        path = self.request.path + (f'?{query}' if query else '')
        return absolute_api_url(self.request, path)

    def get_next_link(self):
        return self._page_link(self.page.next_page_number()) if self.page.has_next() else None

    def get_previous_link(self):
        return self._page_link(self.page.previous_page_number()) if self.page.has_previous() else None


def _song_card_queryset():
    return Song.objects.filter(status=Song.STATUS_PUBLISHED).select_related(
        'artist', 'album', 'uploader'
    ).prefetch_related('featured_artists', 'genres', 'sub_genres', 'moods', 'tags')


def _history_queryset(user):
    return UserHistory.objects.filter(user=user).select_related(
        'song__artist', 'song__album', 'song__uploader', 'album__artist',
        'playlist', 'artist', 'target_user', 'target_user__image_profile',
    ).prefetch_related(
        'song__featured_artists', 'song__genres', 'song__sub_genres', 'song__moods', 'song__tags',
        'album__genres', 'album__sub_genres', 'album__moods',
        'album__songs__artist', 'album__songs__featured_artists', 'album__songs__genres',
        'album__songs__sub_genres', 'album__songs__moods', 'album__songs__tags',
        'playlist__songs__artist', 'playlist__songs__featured_artists', 'playlist__songs__genres',
        'playlist__songs__sub_genres', 'playlist__songs__moods', 'playlist__songs__tags',
        'artist__social_account_links__platform',
    ).order_by('-updated_at')


def _prepare_history(entries, user):
    entries = list(entries)
    songs = [item.song for item in entries if item.song_id]
    albums = [item.album for item in entries if item.album_id]
    artists = [item.artist for item in entries if item.artist_id]
    playlists = [item.playlist for item in entries if item.playlist_id]
    hydrate_song_metrics(songs, user, False)
    hydrate_album_metrics(albums, user)
    hydrate_artist_metrics(artists, user)
    hydrate_playlist_metrics(playlists, user)
    target_ids = [item.target_user_id for item in entries if item.target_user_id]
    followed = set(Follow.objects.filter(
        follower_user=user, followed_user_id__in=target_ids
    ).values_list('followed_user_id', flat=True)) if target_ids else set()
    follower_counts = dict(Follow.objects.filter(followed_user_id__in=target_ids)
        .values('followed_user_id').annotate(total=Count('id')).values_list('followed_user_id','total'))
    for entry in entries:
        if entry.target_user_id:
            entry.target_user._is_following = entry.target_user_id in followed
            entry.target_user._followers_count = follower_counts.get(entry.target_user_id, 0)
    return entries


def _page_values(request, default_size=20, max_size=100):
    try:
        page = max(1, int(request.query_params.get('page', 1)))
        size = max(1, min(int(request.query_params.get('page_size', default_size)), max_size))
        return page, size
    except (TypeError, ValueError):
        return 1, default_size


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


def _clean_string_list(lst):
    """Remove empty or whitespace-only strings from a list.

    Used when incoming payload may include stray empty values (e.g. [""])
    which should not be persisted. Returns a new list; if all items are
    filtered out the result will be empty.
    """
    if not lst:
        return []
    return [str(item) for item in lst if item and str(item).strip()]


def _normalize_id_list(value):
    """Normalize incoming id list values from multipart/form-data or JSON.

    Accepts:
    - a list of strings/ints -> returns list of ints
    - a list containing another list (fix for QueryDict quirk) -> flattens and returns list of ints
    - a single string containing JSON array -> returns list of ints
    - a comma-separated string like "1,2" -> returns list of ints
    - a single numeric string or int -> returns [int]
    - returns None for empty/invalid
    """
    if value is None:
        return None

    # If already a list, try to flatten and parse elements
    if isinstance(value, list):
        out = []
        for v in value:
            # Flatten if nested list (Django QueryDict quirk when manually setting list values)
            if isinstance(v, list):
                out.extend(v)
                continue
            # If element looks like a JSON array string, parse it
            if isinstance(v, str) and v.startswith('[') and v.endswith(']'):
                try:
                    parsed = json.loads(v)
                    out.extend(parsed if isinstance(parsed, list) else [parsed])
                    continue
                except Exception:
                    pass
            # If comma-separated string inside list element
            if isinstance(v, str) and ',' in v:
                parts = [p.strip() for p in v.split(',') if p.strip()]
                out.extend(parts)
                continue
            out.append(v)
        value = out

    # If string, try JSON decode or comma split
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.startswith('[') and s.endswith(']'):
            try:
                parsed = json.loads(s)
                value = parsed
            except Exception:
                # fall through to comma-split
                pass
        elif ',' in s:
            value = [p.strip() for p in s.split(',') if p.strip()]
        else:
            # single scalar string
            value = [s]

    # Now expect iterable
    try:
        iter(value)
    except TypeError:
        return None

    out_ids = []
    for item in value:
        if item is None or item == '':
            continue
        try:
            out_ids.append(int(item))
        except Exception:
            # ignore non-integer items
            try:
                # sometimes items are dicts with id
                if isinstance(item, dict) and 'id' in item:
                    out_ids.append(int(item['id']))
            except Exception:
                continue

    return out_ids if out_ids else None


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
        serializer = UserSerializer(request.user, context={'request': request})
        data = serializer.data
        
        # Add 'image' field for main user from image_profile
        data['image'] = ""
        try:
            if hasattr(request.user, 'image_profile') and request.user.image_profile.status == 'published' and request.user.image_profile.image:
                data['image'] = absolute_api_url(request, request.user.image_profile.image.url)
        except Exception:
            pass

        # Patch 'image' field for user items in followers and following lists
        user_ids_to_fetch = []
        for key in ['followers', 'following']:
            if key in data and isinstance(data[key], dict) and 'items' in data[key]:
                for item in data[key]['items']:
                    if item.get('type') == 'user':
                        user_ids_to_fetch.append(item.get('id'))
        
        if user_ids_to_fetch:
            profiles = {
                p.user_id: p for p in UserImageProfile.objects.filter(
                    user_id__in=user_ids_to_fetch, 
                    status='published'
                ).only('user_id', 'image')
            }
            for key in ['followers', 'following']:
                if key in data and isinstance(data[key], dict) and 'items' in data[key]:
                    for item in data[key]['items']:
                        if item.get('type') == 'user':
                            profile = profiles.get(item.get('id'))
                            if profile and profile.image:
                                item['image'] = absolute_api_url(request, profile.image.url)

        return Response(data)

    @extend_schema(
        summary="ویرایش پروفایل کاربر",
        description="به‌روزرسانی اطلاعات پروفایل کاربر فعلی.",
        request=UserSerializer,
        responses={200: UserSerializer}
    )
    def patch(self, request):
        serializer = UserSerializer(request.user, data=request.data, partial=True, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserImageProfileView(APIView):
    """View for direct upload of user image profile."""
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)

    @extend_schema(
        summary="آپلود تصویر پروفایل کاربر",
        description="آپلود تصویر پروفایل. اگر کاربر از قبل تصویری داشته باشد، تصویر قدیمی حذف و رکورد جدید جایگزین می‌شود.",
        request=UserImageProfileSerializer,
        responses={201: UserImageProfileSerializer}
    )
    def post(self, request, *args, **kwargs):
        # Remove existing record if any as per requirements
        existing_profile = UserImageProfile.objects.filter(user=request.user).first()
        if existing_profile:
            if existing_profile.image:
                try:
                    if os.path.isfile(existing_profile.image.path):
                        os.remove(existing_profile.image.path)
                except (ValueError, FileNotFoundError, NotImplementedError):
                    pass
            existing_profile.delete()
        
        serializer = UserImageProfileSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save(user=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="مشاهده تصویر پروفایل کاربر",
        description="دریافت اطلاعات تصویر پروفایل کاربر فعلی.",
        responses={200: UserImageProfileSerializer}
    )
    def get(self, request, *args, **kwargs):
        profile = get_object_or_404(UserImageProfile, user=request.user)
        serializer = UserImageProfileSerializer(profile)
        return Response(serializer.data)


class UserImageProfileDetailView(APIView):
    """View for deleting user image profile."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="حذف تصویر پروفایل کاربر",
        description="حذف تصویر پروفایل کاربر فعلی.",
        responses={204: None}
    )
    def delete(self, request, *args, **kwargs):
        profile = get_object_or_404(UserImageProfile, user=request.user)
        if profile.image:
            try:
                if os.path.isfile(profile.image.path):
                    os.remove(profile.image.path)
            except (ValueError, FileNotFoundError, NotImplementedError):
                pass
        profile.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class InitialCheckView(APIView):
    """GET and POST initial user genre preferences."""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(
        summary="دریافت چک اولیه کاربر",
        description="دریافت لیست سبک‌های انتخاب شده توسط کاربر در اولین ورود.",
        responses={200: InitialCheckSerializer}
    )
    def get(self, request):
        initial_check = get_object_or_404(InitialCheck, user=request.user)
        serializer = InitialCheckSerializer(initial_check)
        return Response(serializer.data)

    @extend_schema(
        summary="ذخیره چک اولیه کاربر",
        description="ذخیره لیست سبک‌های مورد علاقه در اولین ورود.",
        request=InitialCheckSerializer,
        responses={201: InitialCheckSerializer}
    )
    def post(self, request):
        # We handle both update and create in the serializer's create method
        serializer = InitialCheckSerializer(data=request.data, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
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
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = SongLike.objects.filter(user=request.user).select_related(
            'song__artist', 'song__album', 'song__uploader'
        ).prefetch_related('song__featured_artists', 'song__genres', 'song__sub_genres', 'song__moods', 'song__tags').order_by('-created_at')
        paginator = PageNumberPagination(); paginator.page_size = 10
        page = list(paginator.paginate_queryset(queryset, request))
        hydrate_song_metrics([item.song for item in page], request.user, False)
        return paginator.get_paginated_response(LikedSongSerializer(page, many=True, context={'request': request}).data)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class LikedAlbumsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        songs = _song_card_queryset()
        queryset = AlbumLike.objects.filter(user=request.user).select_related('album__artist').prefetch_related(
            'album__genres', 'album__sub_genres', 'album__moods', Prefetch('album__songs', queryset=songs)
        ).order_by('-created_at')
        paginator = PageNumberPagination(); paginator.page_size = 10
        page = list(paginator.paginate_queryset(queryset, request))
        albums = [item.album for item in page]
        hydrate_album_metrics(albums, request.user)
        all_songs = [song for album in albums for song in album.songs.all()]
        hydrate_song_metrics(all_songs, request.user, False)
        return paginator.get_paginated_response(LikedAlbumSerializer(page, many=True, context={'request': request}).data)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class LikedPlaylistsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        page, page_size = _page_values(request, 10, 50)
        take = page * page_size + 1
        songs = _song_card_queryset()
        admin = list(PlaylistLike.objects.filter(user=request.user).select_related('playlist').prefetch_related(
            'playlist__genres', 'playlist__moods', 'playlist__tags', Prefetch('playlist__songs', queryset=songs)
        ).order_by('-created_at')[:take])
        users = list(UserPlaylist.objects.filter(liked_by=request.user).select_related('user').prefetch_related(
            Prefetch('songs', queryset=songs)
        ).order_by('-created_at')[:take])
        recommended = list(RecommendedPlaylist.objects.filter(liked_by=request.user).select_related('playlist_ref').prefetch_related(
            Prefetch('songs', queryset=songs)
        ).order_by('-created_at')[:take])
        merged = [(x.created_at, 'admin', x) for x in admin] + [(x.created_at, 'user', x) for x in users] + [(x.created_at, 'recommended', x) for x in recommended]
        merged.sort(key=lambda item: item[0], reverse=True)
        start = (page - 1) * page_size; selected = merged[start:start + page_size]
        admin_playlists = [item.playlist for _, kind, item in selected if kind == 'admin']
        hydrate_playlist_metrics(admin_playlists, request.user)
        recommended_items = [item for _, kind, item in selected if kind == 'recommended']
        _attach_recommended_metrics(recommended_items, request.user)
        user_items = [item for _, kind, item in selected if kind == 'user']
        _prepare_user_playlists(user_items, request.user)
        payload = []
        for liked_at, kind, item in selected:
            if kind == 'admin':
                data = PlaylistSerializer(item.playlist, context={'request': request}).data
            elif kind == 'user':
                data = UserPlaylistSerializer(item, context={'request': request}).data
            else:
                data = PlaylistSummarySerializer(item, context={'request': request}).data
            data['liked_at'] = liked_at
            payload.append(data)
        return Response({'count': len(merged), 'next': page + 1 if len(merged) > start + page_size else None,
                         'previous': page - 1 if page > 1 else None, 'results': payload})



@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class MyArtistsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Follow.objects.filter(follower_user=request.user, followed_artist__isnull=False).select_related(
            'followed_artist'
        ).prefetch_related('followed_artist__social_account_links__platform').order_by('-created_at')
        paginator = PageNumberPagination(); paginator.page_size = 10
        page = list(paginator.paginate_queryset(queryset, request))
        artists = [item.followed_artist for item in page]
        hydrate_artist_metrics(artists, request.user)
        data = ArtistSummarySerializer(artists, many=True, context={'request': request}).data
        for row, follow in zip(data, page): row['followed_at'] = follow.created_at
        return paginator.get_paginated_response(data)


@extend_schema(tags=['Library Page Endpoints اندپوینت های صفحه کتابخانه'])
class MyLibraryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        content_type = request.query_params.get('type')
        allowed = {value for value, _ in UserHistory.TYPE_CHOICES}
        if content_type and content_type not in allowed:
            return Response({'detail': 'Invalid type.'}, status=status.HTTP_400_BAD_REQUEST)
        page, page_size = _page_values(request, 20, 50)
        queryset = _history_queryset(request.user)
        if content_type: queryset = queryset.filter(content_type=content_type)
        total = queryset.count(); offset = (page - 1) * page_size
        items = _prepare_history(queryset[offset:offset + page_size], request.user)
        return Response({'items': UserHistorySerializer(items, many=True, context={'request': request}).data,
                         'total': total, 'page': page, 'has_next': total > offset + page_size})


@extend_schema(tags=['Library Page Endpoints اندپوینت های صفحه کتابخانه'])
class UserHistoryView(generics.ListAPIView):
    serializer_class = UserHistorySerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        queryset = _history_queryset(self.request.user)
        content_type = self.request.query_params.get('type')
        if content_type in {value for value, _ in UserHistory.TYPE_CHOICES}:
            queryset = queryset.filter(content_type=content_type)
        elif content_type:
            queryset = queryset.none()
        return queryset

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        items = _prepare_history(page if page is not None else queryset, request.user)
        data = self.get_serializer(items, many=True).data
        return self.get_paginated_response(data) if page is not None else Response(data)


@extend_schema(tags=['Library Page Endpoints اندپوینت های صفحه کتابخانه'])
class UserHistoryDeleteView(generics.DestroyAPIView):
    """
    Delete a single user history entry. Only the owner may delete their history record.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = UserHistorySerializer

    def get_queryset(self):
        # restrict queryset to entries owned by the authenticated user
        return UserHistory.objects.filter(user=self.request.user)

    def delete(self, request, *args, **kwargs):
        return self.destroy(request, *args, **kwargs)


@extend_schema(tags=['Library Page Endpoints اندپوینت های صفحه کتابخانه'])
class DownloadHistoryView(generics.ListAPIView):
    """
    Manages the user's download history.
    """
    serializer_class = DownloadHistorySerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination

    @extend_schema(
        summary="دریافت تاریخچه دانلودهای کاربر",
        description="دریافت لیست آهنگ‌هایی که کاربر برای دانلود اقدام کرده است، بصورت صفحه‌بندی شده و مرتب شده بر اساس آخرین زمان دانلود.",
        responses={200: DownloadHistorySerializer(many=True)}
    )
    def get_queryset(self):
        return DownloadHistory.objects.filter(user=self.request.user).order_by('-updated_at')

    @extend_schema(
        summary="ثبت درخواست دانلود آهنگ",
        description="ثبت یک آهنگ در تاریخچه دانلودهای کاربر. اگر آهنگ قبلاً دانلود شده باشد، زمان آن بروزرسانی می‌شود تا به ابتدای لیست بیاید.",
        request=inline_serializer(
            name='DownloadRequest',
            fields={'song_id': serializers.IntegerField()}
        ),
        responses={201: DownloadHistorySerializer, 200: DownloadHistorySerializer}
    )
    def post(self, request, *args, **kwargs):
        song_id = request.data.get('song_id')
        if not song_id:
            return Response({"error": "song_id is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        song = get_object_or_404(Song, id=song_id)
        
        obj, created = DownloadHistory.objects.update_or_create(
            user=request.user,
            song=song,
            defaults={'updated_at': timezone.now()}
        )
        
        serializer = self.get_serializer(obj)
        if created:
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        else:
            return Response(serializer.data, status=status.HTTP_200_OK)


@extend_schema(tags=['Library Page Endpoints اندپوینت های صفحه کتابخانه'])
class DownloadHistoryDeleteView(generics.DestroyAPIView):
    """
    Delete a single download history entry for the authenticated user.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = DownloadHistorySerializer

    def get_queryset(self):
        return DownloadHistory.objects.filter(user=self.request.user)

    def delete(self, request, *args, **kwargs):
        return self.destroy(request, *args, **kwargs)


@extend_schema(tags=['Library Page Endpoints اندپوینت های صفحه کتابخانه'])
class UserHistorySearchView(UserHistoryView):
    def get_queryset(self):
        queryset = super().get_queryset()
        query = self.request.query_params.get('q', '').strip()
        date_from = self.request.query_params.get('date_from')
        date_to = self.request.query_params.get('date_to')
        if date_from: queryset = queryset.filter(updated_at__date__gte=date_from)
        if date_to: queryset = queryset.filter(updated_at__date__lte=date_to)
        if query:
            queryset = queryset.filter(
                Q(song__title__icontains=query) | Q(song__title_en__icontains=query) |
                Q(song__artist__name__icontains=query) | Q(song__artist__name_en__icontains=query) |
                Q(album__title__icontains=query) | Q(album__title_en__icontains=query) |
                Q(album__artist__name__icontains=query) | Q(album__artist__name_en__icontains=query) |
                Q(playlist__title__icontains=query) | Q(playlist__title_en__icontains=query) |
                Q(playlist__songs__title__icontains=query) | Q(playlist__songs__title_en__icontains=query) |
                Q(playlist__songs__artist__name__icontains=query) | Q(playlist__songs__artist__name_en__icontains=query) |
                Q(artist__name__icontains=query) | Q(artist__name_en__icontains=query) |
                Q(artist__artistic_name__icontains=query) | Q(artist__artistic_name_en__icontains=query) |
                Q(target_user__unique_id__icontains=query) |
                Q(target_user__first_name__icontains=query) | Q(target_user__last_name__icontains=query)
            ).distinct()
        return queryset


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
            featured_ids = data.get('featured_artist_ids', [])
            featured_artists = Artist.objects.filter(id__in=featured_ids)
            featured_names = [a.artistic_name or a.name for a in featured_artists]
            
            artist_name = artist.artistic_name or artist.name
            if featured_names:
                filename_base = f"{artist_name} - {title} (feat. {', '.join(featured_names)})"
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
                # Keep original name and format for cover image
                cover_url, _ = upload_file_to_r2(
                    cover_file,
                    folder='covers'
                )
            
            # Create song record
            # featured_artists is handled via M2M later
            song_data = {
                'title': title,
                'title_en': data.get('title_en', ''),
                'artist': artist,
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
                'description_en': data.get('description_en', ''),
                'lyrics': data.get('lyrics', ''),
                'lyrics_en': data.get('lyrics_en', ''),
                'tempo': data.get('tempo'),
                'energy': data.get('energy'),
                'danceability': data.get('danceability'),
                'valence': data.get('valence'),
                'acousticness': data.get('acousticness'),
                'instrumentalness': data.get('instrumentalness'),
                'speechiness': data.get('speechiness'),
                'live_performed': data.get('live_performed', False),
                'label': data.get('label', ''),
                'label_en': data.get('label_en', ''),
                'producers': data.get('producers', []),
                'producers_en': data.get('producers_en', []),
                'composers': data.get('composers', []),
                'composers_en': data.get('composers_en', []),
                'lyricists': data.get('lyricists', []),
                'lyricists_en': data.get('lyricists_en', []),
                'credits': data.get('credits', ''),
                'credits_en': data.get('credits_en', ''),
            }
            print(f"DEBUG: SongUploadView: Final song_data: {song_data}")
            
            # Add album if provided
            if data.get('album_id'):
                song_data['album'] = Album.objects.get(id=data['album_id'])
            
            song = Song.objects.create(**song_data)
            
            # Add many-to-many relationships
            if featured_ids:
                song.featured_artists.set(featured_artists)
            
            if data.get('genre_ids'):
                song.genres.set(Genre.objects.filter(id__in=data['genre_ids']))
            if data.get('sub_genre_ids'):
                song.sub_genres.set(SubGenre.objects.filter(id__in=data['sub_genre_ids']))
            if data.get('mood_ids'):
                song.moods.set(Mood.objects.filter(id__in=data['mood_ids']))
            if data.get('tag_ids'):
                song.tags.set(Tag.objects.filter(id__in=data['tag_ids']))
            
            return Response(
                SongSerializer(song, context={'request': request}).data,
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
        description="دریافت لیست تمامی هنرمندان ثبت شده در سامانه. Supports `q` (search) and `unlinked` query params.",
        parameters=[
            OpenApiParameter('q', OpenApiTypes.STR, description='Search query (spaces ignored, partial match)'),
            OpenApiParameter('unlinked', OpenApiTypes.BOOL, description='If true, return only artists without a linked user')
        ],
        responses={200: ArtistSerializer(many=True)}
    )
    def get(self, request):
        """List artists. Query params:
        - `q`: text to search in `name` and `artistic_name` (spaces ignored)
        - `unlinked`: boolean; if true only include artists with `user IS NULL`.
        """
        qs = Artist.objects.all()

        # unlinked filter
        unlinked_val = request.query_params.get('unlinked')
        if unlinked_val is not None:
            try:
                if isinstance(unlinked_val, bool):
                    unlinked = unlinked_val
                else:
                    unlinked = str(unlinked_val).lower() in ('1', 'true', 'yes', 'on')
            except Exception:
                unlinked = False
            if unlinked:
                qs = qs.filter(user__isnull=True)

        # search query: ignore spaces in both stored fields and query
        q = request.query_params.get('q') or request.query_params.get('query')
        if q:
            q_norm = ''.join(q.split()).lower()
            # build combined field (name + artistic_name), remove spaces and lowercase
            qs = qs.annotate(
                _combined=Lower(
                    Replace(
                        Concat(F('name'), Value(' '), F('artistic_name')),
                        Value(' '),
                        Value('')
                    )
                )
            ).filter(_combined__contains=q_norm)

        serializer = ArtistSerializer(qs, many=True, context={'request': request})
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
    def get_permissions(self): return [AllowAny()] if self.request.method == 'GET' else [IsAuthenticated()]

    def get(self, request, pk):
        playlist = Playlist.objects.prefetch_related(
            'genres', 'moods', 'tags', Prefetch('songs', queryset=_song_card_queryset())
        ).filter(pk=pk).first()
        if not playlist: return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(user=request.user, content_type=UserHistory.TYPE_PLAYLIST,
                                                 playlist=playlist, defaults={'updated_at': timezone.now()})
        songs = list(playlist.songs.all()); hydrate_song_metrics(songs, request.user, False); hydrate_playlist_metrics([playlist], request.user)
        return Response(PlaylistSerializer(playlist, context={'request': request}).data)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class LikedSongsSearchView(APIView):
    """Search liked songs with flexible matching (partial, phrase, multi-token).

    Behavior:
    - `q` parameter is required.
    - Quoted phrases are treated as single tokens (exact substring match).
    - Unquoted words are split and all tokens must match (AND) across any searchable field.
    - Searchable fields: song title, artist name, album title, tag name, lyrics, description.
    - Uses case-insensitive substring matching (`icontains`).
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="جستجوی آهنگ‌های لایک‌شده",
        description="جستجوی انعطاف‌پذیر در میان آهنگ‌های لایک‌شده کاربر.",
        parameters=[
            OpenApiParameter('q', OpenApiTypes.STR, description='Search query (required)')
        ],
        responses={200: LikedSongSerializer(many=True)}
    )
    def get(self, request):
        query = request.query_params.get('q')
        if not query:
            return Response({'error': 'q parameter is required'}, status=status.HTTP_400_BAD_REQUEST)

        # split into tokens, keeping quoted phrases together
        parts = [m[0] or m[1] for m in re.findall(r'"([^"]+)"|(\S+)', query)]

        qs = SongLike.objects.filter(user=request.user).select_related('song__artist', 'song__album').prefetch_related('song__tags')

        for token in parts:
            token = token.strip()
            if not token:
                continue
            
            # Normalize both the search token and fields to ignore spaces/half-spaces
            clean_token = token.replace(' ', '').replace('\u200c', '')
            
            token_q = (
                Q(song__title__icontains=token) | Q(song__title_en__icontains=token) |
                Q(song__artist__name__icontains=token) | Q(song__artist__name_en__icontains=token) |
                Q(song__album__title__icontains=token) | Q(song__album__title_en__icontains=token) |
                Q(song__tags__name__icontains=token) | Q(song__tags__name_en__icontains=token) |
                Q(song__lyrics__icontains=token) | Q(song__lyrics_en__icontains=token) |
                Q(song__description__icontains=token) | Q(song__description_en__icontains=token)
            )
            
            # Added more comprehensive normalized checks
            qs = qs.annotate(
                st_clean=Replace(Replace(Cast('song__title', TextField()), Value(' '), Value(''), output_field=TextField()), Value('\u200c'), Value(''), output_field=TextField()),
                sa_clean=Replace(Replace(Cast('song__artist__name', TextField()), Value(' '), Value(''), output_field=TextField()), Value('\u200c'), Value(''), output_field=TextField()),
                sla_clean=Replace(Replace(Cast('song__album__title', TextField()), Value(' '), Value(''), output_field=TextField()), Value('\u200c'), Value(''), output_field=TextField()),
            )
            token_q |= (
                Q(st_clean__icontains=clean_token) |
                Q(sa_clean__icontains=clean_token) |
                Q(sla_clean__icontains=clean_token)
            )
            
            qs = qs.filter(token_q)

        qs = qs.order_by('-created_at').distinct()

        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(qs, request)
        serializer = LikedSongSerializer(result_page, many=True, context={'request': request})

        return paginator.get_paginated_response(serializer.data)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class LikedAlbumsSearchView(APIView):
    """Search liked albums with flexible matching (partial, phrase, multi-token)."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="جستجوی آلبوم‌های لایک‌شده",
        parameters=[OpenApiParameter('q', OpenApiTypes.STR, description='Search query (required)')],
        responses={200: LikedAlbumSerializer(many=True)}
    )
    def get(self, request):
        query = request.query_params.get('q')
        if not query:
            return Response({'error': 'q parameter is required'}, status=status.HTTP_400_BAD_REQUEST)

        parts = [m[0] or m[1] for m in re.findall(r'"([^"]+)"|(\S+)', query)]

        qs = AlbumLike.objects.filter(user=request.user).select_related('album__artist').prefetch_related('album__genres', 'album__sub_genres', 'album__moods')

        # Annotate with space-removed fields for comprehensive search
        qs = qs.annotate(
            at_clean=Replace(Replace(Cast('album__title', TextField()), Value(' '), Value(''), output_field=TextField()), Value('\u200c'), Value(''), output_field=TextField()),
            aa_clean=Replace(Replace(Cast('album__artist__name', TextField()), Value(' '), Value(''), output_field=TextField()), Value('\u200c'), Value(''), output_field=TextField()),
        )

        for token in parts:
            token = token.strip()
            if not token:
                continue
            
            clean_token = token.replace(' ', '').replace('\u200c', '')
            
            token_q = (
                Q(album__title__icontains=token) | Q(album__title_en__icontains=token) |
                Q(album__artist__name__icontains=token) | Q(album__artist__name_en__icontains=token) |
                Q(album__description__icontains=token) | Q(album__description_en__icontains=token) |
                Q(album__genres__name__icontains=token) | Q(album__genres__name_en__icontains=token) |
                Q(album__sub_genres__name__icontains=token) | Q(album__sub_genres__name_en__icontains=token) |
                Q(album__moods__name__icontains=token) | Q(album__moods__name_en__icontains=token) |
                Q(at_clean__icontains=clean_token) |
                Q(aa_clean__icontains=clean_token)
            )
            qs = qs.filter(token_q)

        qs = qs.order_by('-created_at').distinct()

        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(qs, request)
        serializer = LikedAlbumSerializer(result_page, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class LikedPlaylistsSearchView(APIView):
    """Search liked playlists (Admin, User, Recommended) with flexible matching."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="جستجوی پلی‌لیست‌های لایک‌شده",
        parameters=[OpenApiParameter('q', OpenApiTypes.STR, description='Search query (required)')],
        responses={200: SimplePlaylistSerializer(many=True)}
    )
    def get(self, request):
        query = request.query_params.get('q')
        if not query:
            return Response({'error': 'q parameter is required'}, status=status.HTTP_400_BAD_REQUEST)

        parts = [m[0] or m[1] for m in re.findall(r'"([^"]+)"|(\S+)', query)]
        user = request.user

        # Fetch and filter each type
        # 1. Admin Playlists (via PlaylistLike)
        liked_admin_ids = PlaylistLike.objects.filter(user=user).values_list('playlist_id', flat=True)
        p_qs = Playlist.objects.filter(id__in=liked_admin_ids).distinct()
        for token in parts:
            token = token.strip()
            if not token: continue
            q = (Q(title__icontains=token) | Q(title_en__icontains=token) | Q(description__icontains=token) | Q(description_en__icontains=token) | Q(songs__title__icontains=token) | Q(songs__title_en__icontains=token) | Q(songs__artist__name__icontains=token) | Q(songs__artist__name_en__icontains=token))
            p_qs = p_qs.filter(q)
        
        # 2. User Playlists
        up_qs = UserPlaylist.objects.filter(liked_by=user).distinct()
        for token in parts:
            token = token.strip()
            if not token: continue
            q = Q(title__icontains=token) | Q(songs__title__icontains=token) | Q(songs__title_en__icontains=token) | Q(songs__artist__name__icontains=token) | Q(songs__artist__name_en__icontains=token)
            up_qs = up_qs.filter(q)
            
        # 3. Recommended Playlists
        rp_qs = RecommendedPlaylist.objects.filter(liked_by=user).distinct()
        for token in parts:
            token = token.strip()
            if not token: continue
            q = (Q(title__icontains=token) | Q(title_en__icontains=token) | Q(description__icontains=token) | Q(description_en__icontains=token) | Q(songs__title__icontains=token) | Q(songs__title_en__icontains=token) | Q(songs__artist__name__icontains=token) | Q(songs__artist__name_en__icontains=token))
            rp_qs = rp_qs.filter(q)

        # Collect and serialize
        results = []
        for p in p_qs:
            results.append(SimplePlaylistSerializer(p, context={'request': request}).data)
        for up in up_qs:
            results.append(UserPlaylistSerializer(up, context={'request': request}).data)
        for rp in rp_qs:
            results.append(PlaylistSummarySerializer(rp, context={'request': request}).data)
            
        # Since liked_at is not easily searchable across combined results without complex SQL, 
        # we'll sort results by title or keep them grouped. Sorting by title for consistency.
        results.sort(key=lambda x: x.get('title', '').lower())
        
        paginator = PageNumberPagination()
        paginator.page_size = 10
        result_page = paginator.paginate_queryset(results, request)
        return paginator.get_paginated_response(result_page)


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
            "likes_count": PlaylistLike.objects.filter(playlist=playlist).count()
        })


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class ArtistDetailView(APIView):
    def get_permissions(self): return [AllowAny()] if self.request.method == 'GET' else [IsAuthenticated()]

    def get(self, request, pk):
        artist = Artist.objects.prefetch_related('social_account_links__platform').filter(pk=pk).first()
        if not artist: return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(user=request.user, content_type=UserHistory.TYPE_ARTIST,
                                                 artist=artist, defaults={'updated_at': timezone.now()})
        page, page_size = _page_values(request, 10, 50); offset = (page - 1) * page_size
        song_base = _song_card_queryset().filter(artist=artist)
        top = song_base.annotate(total_plays=Coalesce(F('plays'), 0) + Count('play_counts')).order_by('-total_plays', '-created_at')
        latest = song_base.order_by('-release_date', '-created_at')
        albums = Album.objects.filter(artist=artist).exclude(Q(title__iexact='single') | Q(title='سینگل')).select_related('artist').prefetch_related(
            'genres', 'sub_genres', 'moods', Prefetch('songs', queryset=_song_card_queryset())
        ).order_by('-release_date', '-created_at')
        list_type = request.query_params.get('type')
        if list_type in {'top_songs', 'latest_songs'}:
            queryset = top if list_type == 'top_songs' else latest; total = queryset.count(); items = list(queryset[offset:offset+page_size])
            hydrate_song_metrics(items, request.user, False)
            return Response({'items': SongStreamSerializer(items, many=True, context={'request': request}).data,
                             'total': total, 'page': page, 'has_next': total > offset + page_size})
        if list_type == 'albums':
            total = albums.count(); items = list(albums[offset:offset+page_size]); hydrate_album_metrics(items, request.user)
            for album in items: hydrate_song_metrics(list(album.songs.all()), request.user, False)
            return Response({'items': AlbumSerializer(items, many=True, context={'request': request}).data,
                             'total': total, 'page': page, 'has_next': total > offset + page_size})
        top_total, album_total, latest_total = top.count(), albums.count(), latest.count()
        top_items, album_items, latest_items = list(top[:5]), list(albums[:5]), list(latest[:5])
        hydrate_song_metrics(top_items + latest_items, request.user, False); hydrate_album_metrics(album_items, request.user); hydrate_artist_metrics([artist], request.user)
        for album in album_items: hydrate_song_metrics(list(album.songs.all()), request.user, False)
        discovered = list(Playlist.objects.filter(songs__artist=artist).values('id','title','cover_image','created_by').distinct()[:8])
        for item in discovered: item.update(type='playlist', image=item.pop('cover_image'), source=item.pop('created_by'))
        key = stable_cache_key('similar-artists-v7', artist.pk, cache_version(CATALOG_VERSION_KEY), cache_version(AFFINITY_VERSION_KEY))
        similar_ids, _ = cache_get_or_claim(key)
        if similar_ids is None:
            genre_ids = list(song_base.values_list('genres__id', flat=True).exclude(genres__id=None).distinct())
            mood_ids = list(song_base.values_list('moods__id', flat=True).exclude(moods__id=None).distinct())
            candidates = Artist.objects.exclude(pk=artist.pk).annotate(
                genre_overlap=Count('songs__genres', filter=Q(songs__status=Song.STATUS_PUBLISHED, songs__genres__in=genre_ids), distinct=True),
                mood_overlap=Count('songs__moods', filter=Q(songs__status=Song.STATUS_PUBLISHED, songs__moods__in=mood_ids), distinct=True),
                shared_followers=Count('follower_artist_relations__follower_user', filter=Q(follower_artist_relations__follower_user__in=Follow.objects.filter(followed_artist=artist).values('follower_user')), distinct=True),
                shared_listeners=Count('monthly_listener_records__user', filter=Q(monthly_listener_records__user__in=ArtistMonthlyListener.objects.filter(artist=artist).values('user')), distinct=True),
            ).filter(Q(genre_overlap__gt=0)|Q(mood_overlap__gt=0)|Q(shared_followers__gt=0)|Q(shared_listeners__gt=0)).order_by(
                '-genre_overlap','-mood_overlap','-shared_followers','-shared_listeners','-verified'
            )
            similar_ids = list(candidates.values_list('id', flat=True)[:30])
            if not similar_ids: similar_ids = list(Artist.objects.exclude(pk=artist.pk).order_by('-verified','name').values_list('id',flat=True)[:30])
            cache_set(key, similar_ids, getattr(settings,'CACHE_TTL_SIMILAR',90))
        selected_ids = similar_ids[:6]; rows = Artist.objects.filter(pk__in=selected_ids).prefetch_related('social_account_links__platform')
        by_id={x.pk:x for x in rows}; similar=[by_id[x] for x in selected_ids if x in by_id]; hydrate_artist_metrics(similar, request.user)
        base_url=absolute_api_url(request, request.path)
        return Response({'artist': ArtistSerializer(artist, context={'request': request}).data,
            'top_songs': {'items': SongStreamSerializer(top_items,many=True,context={'request':request}).data,'total':top_total,
                          'next_page_link':f'{base_url}?type=top_songs&page=2' if top_total>5 else None},
            'albums': {'items':AlbumSerializer(album_items,many=True,context={'request':request}).data,'total':album_total,
                       'next_page_link':f'{base_url}?type=albums&page=2' if album_total>5 else None},
            'latest_songs': {'items':SongStreamSerializer(latest_items,many=True,context={'request':request}).data,'total':latest_total,
                             'next_page_link':f'{base_url}?type=latest_songs&page=2' if latest_total>5 else None},
            'discovered_on':discovered,'similar_artists':ArtistSummarySerializer(similar,many=True,context={'request':request}).data})

   

@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistSocialAccountsView(APIView):
    """Manage social accounts for the authenticated artist"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        social_accounts = ArtistSocialAccount.objects.filter(artist=artist)
        serializer = ArtistSocialAccountSerializer(social_accounts, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="افزودن حساب اجتماعی جدید",
        description="افزودن یک حساب اجتماعی جدید برای هنرمند.",
        request=ArtistSocialAccountSerializer,
        responses={201: ArtistSocialAccountSerializer}
    )
    def post(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = ArtistSocialAccountSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save(artist=artist)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistSocialAccountDetailView(APIView):
    """Update or delete a specific social account for the authenticated artist"""
    permission_classes = [IsAuthenticated]

    def get_object(self, pk, artist):
        try:
            return ArtistSocialAccount.objects.get(pk=pk, artist=artist)
        except ArtistSocialAccount.DoesNotExist:
            return None

    @extend_schema(
        summary="ویرایش حساب اجتماعی",
        description="ویرایش یک حساب اجتماعی خاص.",
        request=ArtistSocialAccountSerializer,
        responses={200: ArtistSocialAccountSerializer}
    )
    def put(self, request, pk):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        social_account = self.get_object(pk, artist)
        if not social_account:
            return Response({"detail": "Social account not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = ArtistSocialAccountSerializer(social_account, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف حساب اجتماعی",
        description="حذف یک حساب اجتماعی خاص.",
        responses={204: OpenApiTypes.NONE}
    )
    def delete(self, request, pk):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        
        try:
            artist = user.artist_profile
        except Artist.DoesNotExist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        social_account = self.get_object(pk, artist)
        if not social_account:
            return Response({"detail": "Social account not found."}, status=status.HTTP_404_NOT_FOUND)

        social_account.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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
    def get_permissions(self): return [AllowAny()] if self.request.method == 'GET' else [IsAuthenticated()]

    def get(self, request, pk):
        album = Album.objects.select_related('artist').prefetch_related(
            'genres', 'sub_genres', 'moods', Prefetch('songs', queryset=_song_card_queryset(), to_attr='_detail_songs')
        ).filter(pk=pk).first()
        if not album: return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(user=request.user, content_type=UserHistory.TYPE_ALBUM,
                                                 album=album, defaults={'updated_at': timezone.now()})
        hydrate_album_metrics([album], request.user); hydrate_song_metrics(album._detail_songs, request.user, False)
        return Response(AlbumSerializer(album, context={'request': request}).data)

   

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


@extend_schema(tags=['Classification اندپوینت های دسته‌بندی'])
class GenreSongsListView(generics.ListAPIView):
    """List songs associated with a specific genre."""
    permission_classes = [AllowAny]
    serializer_class = SongSummarySerializer
    pagination_class = StandardResultsSetPagination

    @extend_schema(
        summary="لیست آهنگ‌های یک سبک",
        description="دریافت لیست آهنگ‌هایی که با یک سبک موسیقی خاص مرتبط هستند.",
        responses={200: SongSummarySerializer(many=True)}
    )
    def get_queryset(self):
        genre_id = self.kwargs.get('pk')
        genre = get_object_or_404(Genre, pk=genre_id)
        return Song.objects.filter(
            genres=genre, 
            status=Song.STATUS_PUBLISHED
        ).select_related('artist', 'album').prefetch_related('genres', 'tags', 'moods', 'sub_genres')


@extend_schema(tags=['Classification اندپوینت های دسته‌بندی'])
class GenreDetailView(APIView):
    """Retrieve, Update, and Delete Genre"""

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
    def get_permissions(self): return [AllowAny()] if self.request.method == 'GET' else [IsAuthenticated()]

    def get(self, request, pk):
        queryset = Song.objects.select_related('artist', 'album', 'uploader').prefetch_related(
            'featured_artists', 'genres', 'sub_genres', 'moods', 'tags'
        )
        if not request.user.is_authenticated or not request.user.is_staff:
            queryset = queryset.filter(status=Song.STATUS_PUBLISHED)
        song = queryset.filter(pk=pk).first()
        if not song: return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if request.user.is_authenticated:
            UserHistory.objects.update_or_create(user=request.user, content_type=UserHistory.TYPE_SONG,
                                                 song=song, defaults={'updated_at': timezone.now()})
        hydrate_song_metrics([song], request.user)
        data = SongSerializer(song, context={'request': request}).data
        artist_profile = getattr(request.user, 'artist_profile', None) if request.user.is_authenticated else None
        if artist_profile and song.artist_id == artist_profile.id:
            try: days = max(1, min(int(request.query_params.get('days', 30)), 365))
            except (TypeError, ValueError): days = 30
            plays = song.play_counts.filter(created_at__gte=timezone.now() - timedelta(days=days))
            total = plays.count()
            def distribution(field):
                return [{field: row[field], 'count': row['count'],
                         'percentage': round(row['count'] * 100 / total, 2) if total else 0}
                        for row in plays.values(field).annotate(count=Count('id')).order_by('-count')]
            data['analytics'] = {'days': days, 'total_period_plays': total,
                'daily_plays': list(plays.annotate(date=TruncDate('created_at')).values('date').annotate(count=Count('id')).order_by('date')),
                'city_distribution': distribution('city'), 'country_distribution': distribution('country')}
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
        # 1. Global check for pending ads (enforce sequential viewing for FREE users)
        # Check if user has any pending ads (required but not seen) from previous requests
        pending_ad = StreamAccess.objects.filter(user=request.user, ad_required=True, ad_seen=False).select_related('ad_object').first()
        if pending_ad:
            return Response({
                'type': 'ad',
                'ad': AudioAdSerializer(pending_ad.ad_object, context={'request': request}).data,
                'submit_id': pending_ad.ad_submit_id,
                'message': 'You must finish watching the previous advertisement',
                'pending': True,
                'ad_status': 'blocking_pending'
            })

        try:
            # Get the stream access record
            stream_access = StreamAccess.objects.select_related('song', 'user').get(
                unwrap_token=token,
                user=request.user
            )
            
            # Check if already unwrapped
            if stream_access.unwrapped:
                return Response(
                    {'error': 'This stream token has already been used', 'ad_status': 'already_unwrapped'},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Mark as unwrapped (ONLY after passing pending ad check)
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
            config = PlayConfiguration.objects.order_by('-updated_at').first()
            ad_freq = config.ad_frequency if config else 15
            
            # ONLY show ads for FREE users
            is_premium = request.user.plan == User.PLAN_PREMIUM
            
            # Calculate songs since last ad (ignoring the past)
            last_ad_seen = StreamAccess.objects.filter(
                user=request.user, 
                ad_required=True, 
                ad_seen=True
            ).order_by('-unwrapped_at').first()
            
            since_query = Q(user=request.user, unwrapped=True)
            if last_ad_seen and last_ad_seen.unwrapped_at:
                since_query &= Q(unwrapped_at__gt=last_ad_seen.unwrapped_at)
            
            unwrapped_since_last_ad = StreamAccess.objects.filter(since_query).count()

            # Ad decision status for response diagnostic
            ad_status = {
                'since_last_ad': unwrapped_since_last_ad,
                'frequency': ad_freq,
                'is_premium': is_premium,
                'total_24h': unwrapped_count
            }

            if not is_premium and ad_freq > 0 and unwrapped_since_last_ad >= ad_freq:
                # Pick a random active ad
                active_ads = AudioAd.objects.filter(is_active=True)
                if not active_ads.exists():
                    # Fallback: if no active ads, but some ads exist at all, use them
                    active_ads = AudioAd.objects.all()
                
                if active_ads.exists():
                    import random
                    import secrets
                    ad = random.choice(active_ads)
                    submit_id = secrets.token_urlsafe(32)
                    
                    stream_access.ad_required = True
                    stream_access.ad_seen = False
                    stream_access.ad_submit_id = submit_id
                    stream_access.ad_object = ad
                    stream_access.save(update_fields=['ad_required', 'ad_seen', 'ad_submit_id', 'ad_object'])
                    
                    return Response({
                        'type': 'ad',
                        'ad': AudioAdSerializer(ad, context={'request': request}).data,
                        'submit_id': submit_id,
                        'message': 'Please listen to this brief advertisement',
                        'unwrap_count': unwrapped_count,
                        'since_last_ad': unwrapped_since_last_ad,
                        'ad_status': ad_status
                    })
                else:
                    ad_status['error'] = 'No ads available in database'
            
            # No ad required, return stream response
            res = self._get_stream_response(request, stream_access, unwrapped_count)
            if hasattr(res, 'data') and isinstance(res.data, dict):
                res.data['ad_status'] = ad_status
            return res
            
        except StreamAccess.DoesNotExist:
            return Response(
                {'error': 'Invalid or unauthorized stream token'},
                status=status.HTTP_404_NOT_FOUND
            )

    def _get_stream_response(self, request, stream_access, unwrapped_count):
        """Helper to generate the final stream response with quality selection"""
        song = stream_access.song

        # Record history
        UserHistory.objects.update_or_create(
            user=request.user,
            content_type=UserHistory.TYPE_SONG,
            song=song,
            defaults={'updated_at': timezone.now()}
        )
        
        # Quality selection: Use user setting if available
        # if high quality was selected by user we only provide audio_url (128kbps/320kbps usually)
        # but if medium quality was selected, we provide converted_audio_url (128kbps) if available, 
        # otherwise fallback to audio_url
        quality = request.user.stream_quality
        if quality == 'high' or not song.converted_audio_url:
            audio_url = song.audio_file
        else:
            audio_url = song.converted_audio_url

        # Extract path for signing if it's an R2 URL
        cdn_base = getattr(settings, 'R2_CDN_BASE', '').rstrip('/')
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
        # 1. Global check for pending ads (enforce sequential viewing for FREE users)
        pending_ad = StreamAccess.objects.filter(user=request.user, ad_required=True, ad_seen=False).select_related('ad_object').first()
        if pending_ad:
            return Response({
                'type': 'ad',
                'ad': AudioAdSerializer(pending_ad.ad_object, context={'request': request}).data,
                'submit_id': pending_ad.ad_submit_id,
                'message': 'You must finish watching the previous advertisement',
                'pending': True,
                'ad_status': 'blocking_pending'
            })

        try:
            # Get the stream access record
            stream_access = StreamAccess.objects.select_related('song', 'user').get(
                short_token=token,
                user=request.user
            )

            # Use ad frequency from configuration
            config = PlayConfiguration.objects.order_by('-updated_at').first()
            ad_freq = config.ad_frequency if config else 15
            is_premium = request.user.plan == User.PLAN_PREMIUM

            # Check if already unwrapped
            if stream_access.unwrapped:
                # Generate a new short token for this user/song and return it
                from django.urls import reverse
                import secrets
                from uuid import uuid4
                import random

                short_token = None
                for _ in range(6):
                    candidate = secrets.token_urlsafe(6)[:8]
                    if not StreamAccess.objects.filter(short_token=candidate).exists():
                        short_token = candidate
                        break
                if not short_token:
                    short_token = uuid4().hex[:8]

                unique_otplay_id = None
                for _ in range(6):
                    candidate = secrets.token_urlsafe(16)
                    if not StreamAccess.objects.filter(unique_otplay_id=candidate).exists():
                        unique_otplay_id = candidate
                        break
                if not unique_otplay_id:
                    unique_otplay_id = uuid4().hex

                # create a new StreamAccess for this user and same song
                new_sa = StreamAccess.objects.create(
                    user=request.user,
                    song=stream_access.song,
                    short_token=short_token,
                    unique_otplay_id=unique_otplay_id
                )

                # Build new short URL
                new_path = reverse('stream-short', kwargs={'token': short_token})
                new_url = absolute_api_url(request, new_path)

                # Count unwrapped streams for this user (last 24 hours for fairness)
                cutoff_time = timezone.now() - timedelta(hours=24)
                unwrapped_count = StreamAccess.objects.filter(
                    user=request.user,
                    unwrapped=True,
                    unwrapped_at__gte=cutoff_time
                ).count()

                # Calculate songs since last ad
                last_ad_seen = StreamAccess.objects.filter(
                    user=request.user, 
                    ad_required=True, 
                    ad_seen=True
                ).order_by('-unwrapped_at').first()
                
                since_query = Q(user=request.user, unwrapped=True)
                if last_ad_seen and last_ad_seen.unwrapped_at:
                    since_query &= Q(unwrapped_at__gt=last_ad_seen.unwrapped_at)
                
                unwrapped_since_last_ad = StreamAccess.objects.filter(since_query).count()

                # Ad decision status for response diagnostic
                ad_status = {
                    'since_last_ad': unwrapped_since_last_ad,
                    'frequency': ad_freq,
                    'is_premium': is_premium,
                    'total_24h': unwrapped_count,
                    'is_already_unwrapped': True
                }

                if not is_premium and ad_freq > 0 and unwrapped_since_last_ad >= ad_freq:
                    active_ads = AudioAd.objects.filter(is_active=True)
                    if not active_ads.exists():
                        active_ads = AudioAd.objects.all()
                    
                    if active_ads.exists():
                        ad = random.choice(active_ads)
                        submit_id = secrets.token_urlsafe(32)

                        new_sa.ad_required = True
                        new_sa.ad_seen = False
                        new_sa.ad_submit_id = submit_id
                        new_sa.ad_object = ad
                        new_sa.save(update_fields=['ad_required', 'ad_seen', 'ad_submit_id', 'ad_object'])

                        return Response({
                            'type': 'ad',
                            'ad': AudioAdSerializer(ad, context={'request': request}).data,
                            'submit_id': submit_id,
                            'message': 'Please listen to this brief advertisement',
                            'unwrap_count': unwrapped_count,
                            'since_last_ad': unwrapped_since_last_ad,
                            'new_stream_url': new_url,
                            'ad_status': ad_status
                        }, status=413)

                # Otherwise return error with new stream url and HTTP 413
                return Response({
                    'error': 'This stream URL has already been used',
                    'new_stream_url': new_url,
                    'ad_status': ad_status
                }, status=413)

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
            
            # Calculate songs since last ad
            last_ad_seen = StreamAccess.objects.filter(
                user=request.user, 
                ad_required=True, 
                ad_seen=True
            ).order_by('-unwrapped_at').first()
            
            since_query = Q(user=request.user, unwrapped=True)
            if last_ad_seen and last_ad_seen.unwrapped_at:
                since_query &= Q(unwrapped_at__gt=last_ad_seen.unwrapped_at)
            
            unwrapped_since_last_ad = StreamAccess.objects.filter(since_query).count()

            # Ad decision status for response diagnostic
            ad_status = {
                'since_last_ad': unwrapped_since_last_ad,
                'frequency': ad_freq,
                'is_premium': is_premium,
                'total_24h': unwrapped_count
            }

            if not is_premium and ad_freq > 0 and unwrapped_since_last_ad >= ad_freq:
                # Pick a random active ad
                active_ads = AudioAd.objects.filter(is_active=True)
                if not active_ads.exists():
                    active_ads = AudioAd.objects.all()
                
                if active_ads.exists():
                    import random
                    import secrets
                    ad = random.choice(active_ads)
                    submit_id = secrets.token_urlsafe(32)
                    
                    stream_access.ad_required = True
                    stream_access.ad_seen = False
                    stream_access.ad_submit_id = submit_id
                    stream_access.ad_object = ad
                    stream_access.save(update_fields=['ad_required', 'ad_seen', 'ad_submit_id', 'ad_object'])
                    
                    return Response({
                        'type': 'ad',
                        'ad': AudioAdSerializer(ad, context={'request': request}).data,
                        'submit_id': submit_id,
                        'message': 'Please listen to this brief advertisement',
                        'unwrap_count': unwrapped_count,
                        'since_last_ad': unwrapped_since_last_ad,
                        'ad_status': ad_status
                    })
                else:
                    ad_status['error'] = 'No ads available in database'
            
            # No ad required, return stream response
            response = UnwrapStreamView()._get_stream_response(request, stream_access, unwrapped_count)
            if hasattr(response, 'data') and isinstance(response.data, dict):
                response.data['ad_status'] = ad_status
            return response
            
        except StreamAccess.DoesNotExist:
            # Try to find a StreamAccess with this token regardless of user.
            # If found, it means the short link exists but belongs to another user
            # or was expired/removed for this user. Create a new short token for
            # the current user for the same song and return 421 with the new link.
            from django.urls import reverse
            other = StreamAccess.objects.select_related('song').filter(short_token=token).first()
            if other and other.song:
                # generate a new short token and unique_otplay_id
                import secrets
                from uuid import uuid4

                short_token = None
                for _ in range(6):
                    candidate = secrets.token_urlsafe(6)[:8]
                    if not StreamAccess.objects.filter(short_token=candidate).exists():
                        short_token = candidate
                        break
                if not short_token:
                    short_token = uuid4().hex[:8]

                unique_otplay_id = None
                for _ in range(6):
                    candidate = secrets.token_urlsafe(16)
                    if not StreamAccess.objects.filter(unique_otplay_id=candidate).exists():
                        unique_otplay_id = candidate
                        break
                if not unique_otplay_id:
                    unique_otplay_id = uuid4().hex

                # create a new StreamAccess for this user and same song
                new_sa = StreamAccess.objects.create(
                    user=request.user,
                    song=other.song,
                    short_token=short_token,
                    unique_otplay_id=unique_otplay_id
                )

                new_path = reverse('stream-short', kwargs={'token': short_token})
                new_url = absolute_api_url(request, new_path)

                return Response({
                    'error': 'Stream link expired or unauthorized for this user',
                    'message': 'A new short stream link has been generated',
                    'new_stream_url': new_url
                }, status=421)

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


@extend_schema(
    summary="دریافت بنر تبلیغاتی",
    description="یک بنر فعال را به شیوه‌ای چرخان (round-robin) برمی‌گرداند و شمارنده‌ی سروهای بنر را افزایش می‌دهد.",
    responses={200: BannerAdSerializer, 204: None}
)
class BannerAdView(APIView):
    """Public endpoint that returns exactly one banner ad.

    Uses a DB-backed counter (`BannerAdServeCounter`) to atomically
    rotate through active banners so view counts grow in a flat line.
    """
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        from django.db import transaction
        from django.db.models import F

        with transaction.atomic():
            counter, _ = BannerAdServeCounter.objects.select_for_update().get_or_create(pk=1)
            active_ads = list(BannerAd.objects.filter(is_active=True).order_by('created_at'))
            if not active_ads:
                return Response(status=status.HTTP_204_NO_CONTENT)

            n = len(active_ads)
            idx = (counter.total_serves % n) if n > 0 else 0
            ad = active_ads[idx]

            # Increment global counter and selected ad's view_count atomically
            counter.total_serves = F('total_serves') + 1
            counter.save()
            ad.view_count = F('view_count') + 1
            ad.save()
            # refresh to get concrete integers
            ad.refresh_from_db()

        serializer = BannerAdSerializer(ad, context={'request': request})
        return Response(serializer.data)

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
def _prepare_user_playlists(playlists, user=None, songs_attr='_detail_songs'):
    items = list(playlists)
    ids = [item.id for item in items]
    liked = set()
    if ids and user is not None and getattr(user, 'is_authenticated', False):
        liked = set(UserPlaylist.objects.filter(id__in=ids, liked_by=user).values_list('id', flat=True))
    for item in items:
        item._songs_count = getattr(item, 'songs_count_value', len(getattr(item, songs_attr, [])))
        item._likes_count = getattr(item, 'likes_count_value', 0)
        item._is_liked = item.id in liked
        hydrate_song_metrics(getattr(item, songs_attr, []), user if getattr(user, 'is_authenticated', False) else None, False)
    return items


def _user_playlist_queryset():
    return UserPlaylist.objects.select_related('user').annotate(
        songs_count_value=Count('songs', distinct=True),
        likes_count_value=Count('liked_by', distinct=True),
    ).prefetch_related(Prefetch('songs', queryset=_song_card_queryset(), to_attr='_detail_songs'))

class UserPlaylistListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        playlists = _prepare_user_playlists(
            _user_playlist_queryset().filter(user=request.user).order_by('-updated_at'), request.user
        )
        return Response(UserPlaylistSerializer(playlists, many=True, context={'request': request}).data)

    def post(self, request):
        serializer = UserPlaylistCreateSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        playlist = serializer.save()
        playlist = _prepare_user_playlists(_user_playlist_queryset().filter(pk=playlist.pk), request.user)[0]
        return Response(UserPlaylistSerializer(playlist, context={'request': request}).data, status=status.HTTP_201_CREATED)



@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class UserPlaylistDetailView(APIView):
    def get_permissions(self):
        return [AllowAny()] if self.request.method == 'GET' else [IsAuthenticated()]

    def _get(self, pk):
        return _user_playlist_queryset().filter(pk=pk).first()

    def get(self, request, pk):
        playlist = self._get(pk)
        if not playlist or (not playlist.public and (not request.user.is_authenticated or playlist.user_id != request.user.id)):
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        _prepare_user_playlists([playlist], request.user)
        return Response(UserPlaylistSerializer(playlist, context={'request': request}).data)

    def put(self, request, pk):
        playlist = self._get(pk)
        if not playlist or playlist.user_id != request.user.id:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        serializer = UserPlaylistSerializer(playlist, data=request.data, partial=True, context={'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        refreshed = _prepare_user_playlists(_user_playlist_queryset().filter(pk=pk), request.user)[0]
        return Response(UserPlaylistSerializer(refreshed, context={'request': request}).data)

    def delete(self, request, pk):
        deleted, _ = UserPlaylist.objects.filter(pk=pk, user=request.user).delete()
        if not deleted:
            return Response({'error': 'Playlist not found'}, status=status.HTTP_404_NOT_FOUND)
        return Response(status=status.HTTP_204_NO_CONTENT)



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
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)

        # If the song is already present in the playlist return a specific code
        if playlist.songs.filter(id=song.id).exists():
            return Response(
                {'error': 'Song already in playlist', 'code': 'song_already_in_playlist'},
                status=status.HTTP_409_CONFLICT
            )

        playlist.songs.add(song)
        # Maintain playlist.order JSON (append new song id if not present)
        try:
            order = playlist.order or []
            if not isinstance(order, list):
                order = list(order)
        except Exception:
            order = []

        if song.id not in order:
            order.append(song.id)
            playlist.order = order
            playlist.save(update_fields=['order'])

        serializer = UserPlaylistSerializer(playlist, context={'request': request})
        return Response(serializer.data)


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
            # Update playlist.order to remove this song id if present
            try:
                order = playlist.order or []
                if not isinstance(order, list):
                    order = list(order)
            except Exception:
                order = []

            if song.id in order:
                try:
                    order.remove(song.id)
                except ValueError:
                    pass
                playlist.order = order
                playlist.save(update_fields=['order'])

            serializer = UserPlaylistSerializer(playlist, context={'request': request})
            return Response(serializer.data)
        except Song.DoesNotExist:
            return Response({'error': 'Song not found'}, status=status.HTTP_404_NOT_FOUND)


@extend_schema(tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'])
class UserPlaylistLikeView(APIView):
    """Like or unlike a user-created playlist (toggle)."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="لایک یا لغو لایک پلی‌لیست کاربر",
        description="لایک یا لغو لایک یک پلی‌لیست ساخته شده توسط کاربر.",
        responses={200: inline_serializer(name='UserPlaylistLikeResponse', fields={
            'liked': serializers.BooleanField(),
            'likes_count': serializers.IntegerField()
        })}
    )
    def post(self, request, pk):
        try:
            playlist = UserPlaylist.objects.get(pk=pk)
        except UserPlaylist.DoesNotExist:
            return Response({'detail': 'Playlist not found.'}, status=status.HTTP_404_NOT_FOUND)

        user = request.user
        # Toggle membership in M2M `liked_by`
        if playlist.liked_by.filter(id=user.id).exists():
            playlist.liked_by.remove(user)
            liked = False
        else:
            playlist.liked_by.add(user)
            liked = True

        return Response({'liked': liked, 'likes_count': playlist.liked_by.count()})


class UserProfilePublicView(APIView):
    """
    Public profile of a normal user.
    """
    permission_classes = [AllowAny]

    @extend_schema(
        summary="مشاهده پروفایل عمومی کاربر",
        description="دریافت اطلاعات عمومی یک کاربر معمولی شامل آمار فالوورها و پلی‌لیست‌های او. شناسه منحصر‌به‌فرد کاربر (unique_id) به عنوان ورودی استفاده می‌شود.",
        tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'],
        responses={200: UserPublicProfileSerializer}
    )
    def get(self, request, unique_id):
        user = get_object_or_404(User, unique_id=unique_id)
        # If caller requests followers/following lists via query params, return paginated lists.
        # Supported params:
        # - followers=1 : return followers page using f_page & f_page_size
        # - following=1 : return following page using fg_page & fg_page_size
        # If neither provided, return standard public profile serializer.
        include_followers = request.query_params.get('followers') is not None
        include_following = request.query_params.get('following') is not None

        if include_followers or include_following:
            from .serializers import FollowableEntitySerializer
            result = {}
            if include_followers:
                # pagination params for followers
                try:
                    page = int(request.query_params.get('f_page', 1))
                    page_size = int(request.query_params.get('f_page_size', 10))
                except (ValueError, TypeError):
                    page, page_size = 1, 10

                offset = (page - 1) * page_size
                qs = Follow.objects.filter(followed_user=user).select_related('follower_user', 'follower_user__image_profile', 'follower_artist').order_by('-created_at')
                total = qs.count()
                items = [f.follower_user or f.follower_artist for f in qs[offset:offset + page_size]]
                has_next = total > offset + page_size

                next_url = None
                if request and has_next:
                    try:
                        base = reverse('user_public_profile', kwargs={'unique_id': unique_id})
                    except Exception:
                        base = request.path
                    params = request.query_params.copy()
                    params['f_page'] = str(page + 1)
                    params['f_page_size'] = str(page_size)
                    next_url = absolute_api_url(request, base + '?' + params.urlencode())

                items_data = FollowableEntitySerializer(items, many=True, context={'request': request}).data
                for i, item_data in enumerate(items_data):
                    if item_data.get('type') == 'user':
                        user_obj = items[i]
                        try:
                            if hasattr(user_obj, 'image_profile') and user_obj.image_profile.status == 'published' and user_obj.image_profile.image:
                                item_data['image'] = absolute_api_url(request, user_obj.image_profile.image.url)
                        except Exception: pass

                result['followers'] = {
                    'items': items_data,
                    'total': total,
                    'page': page,
                    'has_next': has_next,
                    'next': next_url,
                }

            if include_following:
                # pagination params for following
                try:
                    page = int(request.query_params.get('fg_page', 1))
                    page_size = int(request.query_params.get('fg_page_size', 10))
                except (ValueError, TypeError):
                    page, page_size = 1, 10

                offset = (page - 1) * page_size
                qs = Follow.objects.filter(follower_user=user).select_related('followed_user', 'followed_user__image_profile', 'followed_artist').order_by('-created_at')
                total = qs.count()
                items = [f.followed_user or f.followed_artist for f in qs[offset:offset + page_size]]
                has_next = total > offset + page_size

                next_url = None
                if request and has_next:
                    try:
                        base = reverse('user_public_profile', kwargs={'unique_id': unique_id})
                    except Exception:
                        base = request.path
                    params = request.query_params.copy()
                    params['fg_page'] = str(page + 1)
                    params['fg_page_size'] = str(page_size)
                    next_url = absolute_api_url(request, base + '?' + params.urlencode())

                items_data = FollowableEntitySerializer(items, many=True, context={'request': request}).data
                for i, item_data in enumerate(items_data):
                    if item_data.get('type') == 'user':
                        user_obj = items[i]
                        try:
                            if hasattr(user_obj, 'image_profile') and user_obj.image_profile.status == 'published' and user_obj.image_profile.image:
                                item_data['image'] = absolute_api_url(request, user_obj.image_profile.image.url)
                        except Exception: pass

                result['following'] = {
                    'items': items_data,
                    'total': total,
                    'page': page,
                    'has_next': has_next,
                    'next': next_url,
                }

            return Response(result)

        # default: full public profile
        # Record profile view in history (skip if anonymous or viewing own profile)
        if request.user.is_authenticated and request.user.id != user.id:
            UserHistory.objects.update_or_create(
                user=request.user,
                content_type=UserHistory.TYPE_USER,
                target_user=user,
                defaults={'updated_at': timezone.now()}
            )

        serializer = UserPublicProfileSerializer(user, context={'request': request})
        data = serializer.data
        
        # Add 'image' field for main user from image_profile
        data['image'] = ""
        try:
            if hasattr(user, 'image_profile') and user.image_profile.status == 'published' and user.image_profile.image:
                data['image'] = absolute_api_url(request, user.image_profile.image.url)
        except Exception:
            pass

        return Response(data)


def _sedabox_user():
    users = User.objects.select_related('image_profile')
    return users.filter(unique_id='sedabox').first() or users.filter(
        Q(first_name='SedaBox |', last_name='صداباکس') | Q(last_name='صداباکس')
    ).order_by('id').first()


def _sedabox_unique_id():
    key = stable_cache_key(
        'sedabox-user-id', cache_version(USER_DIRECTORY_VERSION_KEY), 'v1',
    )
    value = cache_get(key)
    if value is None:
        value = User.objects.filter(unique_id='sedabox').values_list(
            'unique_id', flat=True,
        ).first() or User.objects.filter(
            first_name='SedaBox |', last_name='صداباکس',
        ).values_list('unique_id', flat=True).first() or 'sedabox'
        cache_set(key, value, 600)
    return value


def _sedabox_normal_playlist_queryset(request):
    authenticated = request.user.is_authenticated
    song_filter = Q(songs__status=Song.STATUS_PUBLISHED)
    if not authenticated:
        song_filter &= Q(songs__preview_audio_url__isnull=False) & ~Q(songs__preview_audio_url='')
    song_qs = _home_song_queryset(not authenticated).order_by('-release_date', '-created_at')
    return Playlist.objects.annotate(
        songs_count_value=Count('songs', filter=song_filter, distinct=True)
    ).filter(songs_count_value__gt=0).prefetch_related(
        'genres', 'moods',
        Prefetch('songs', queryset=song_qs, to_attr='_card_songs'),
    ).order_by('-created_at')


def _sedabox_preview_payload(request, user, page_size=3):
    generated_all = _dynamic_playlist_items(request.user)
    generated = generated_all[:min(2, page_size)]
    remaining = max(0, page_size - len(generated))

    normal_qs = _sedabox_normal_playlist_queryset(request)
    normal = list(normal_qs[:remaining]) if remaining else []
    hydrate_playlist_metrics(normal, request.user if request.user.is_authenticated else None)
    for playlist in normal:
        playlist._songs_count = playlist.songs_count_value
        playlist._creator_unique_id = 'sedabox'
    _attach_recommended_metrics(generated, request.user)

    image_profile = None
    try:
        if user.image_profile.status == 'published' and user.image_profile.image:
            image_profile = {
                'id': user.image_profile.id,
                'image': absolute_api_url(request, user.image_profile.image.url),
                'status': user.image_profile.status,
            }
    except Exception:
        pass

    results = list(PlaylistSummarySerializer(
        generated, many=True, context={'request': request}
    ).data)
    results.extend(SimplePlaylistSerializer(
        normal, many=True, context={'request': request}
    ).data)
    total = normal_qs.count() + _home_playlist_queryset(request.user).count() + len(generated_all)
    return {
        'id': user.id,
        'unique_id': 'sedabox',
        'first_name': user.first_name,
        'last_name': user.last_name,
        'followers_count': Follow.objects.filter(followed_user=user).count(),
        'image_profile': image_profile,
        'user_playlists': {
            'count': len(results),
            'total': total,
            'next': None,
            'results': results,
        },
    }


class SedaBoxProfileView(APIView):
    """
    SedaBox (platform) profile view.
    Structure matches a normal user's public profile, but populates 
    `user_playlists` from all platform Sources (Admin/System/Event/Recommended).
    """
    permission_classes = [AllowAny]

    @extend_schema(
        summary="SedaBox Platform Profile",
        description="Returns the profile details and all public playlists for the SedaBox platform user.",
        tags=['Profile Page Endpoints اندپوینت های صفحه پروفایل'],
        responses={200: UserPublicProfileSerializer}
    )
    def get(self, request):
        user = _sedabox_user()
        if not user:
            return Response({"error": "SedaBox user not found"}, status=status.HTTP_404_NOT_FOUND)
        if str(request.query_params.get('preview', '')).lower() in {'1', 'true', 'yes'}:
            try:
                page_size = max(1, min(int(request.query_params.get('page_size', 3)), 4))
            except (TypeError, ValueError):
                page_size = 3
            key = stable_cache_key(
                'sedabox-profile-preview', get_request_language(request), not request.user.is_authenticated, page_size,
                cache_version(CATALOG_VERSION_KEY), cache_version(USER_DIRECTORY_VERSION_KEY), 'v3',
            )
            cached = cache_get(key) if not request.user.is_authenticated else None
            if cached is not None:
                return Response(cached)
            payload = _sedabox_preview_payload(request, user, page_size)
            if not request.user.is_authenticated:
                cache_set(key, payload, 120)
            return Response(payload)
            
        # Standard profile fields; playlist results are assembled below.
        user_serializer = UserPublicProfileSerializer(user, context={'request': request})
        profile_data = user_serializer.data
        profile_data['unique_id'] = 'sedabox'

        if request.user.is_authenticated and request.user.id != user.id:
            UserHistory.objects.update_or_create(
                user=request.user,
                content_type=UserHistory.TYPE_USER,
                target_user=user,
                defaults={'updated_at': timezone.now()},
            )

        page, page_size = _page_values(request, default_size=20, max_size=100)
        end = page * page_size

        normal_qs = _sedabox_normal_playlist_queryset(request)
        normal_total = normal_qs.count()
        normal = list(normal_qs[:end])
        hydrate_playlist_metrics(normal, request.user if request.user.is_authenticated else None)
        for playlist in normal:
            playlist._songs_count = playlist.songs_count_value
            playlist._creator_unique_id = 'sedabox'

        generated = _dynamic_playlist_items(request.user)
        recommended_qs = _home_playlist_queryset(request.user).order_by('-updated_at', '-created_at')
        recommended_total = recommended_qs.count()
        recommended = generated + list(recommended_qs[:end])
        _attach_recommended_metrics(recommended, request.user)

        def sort_time(obj):
            return getattr(obj, 'updated_at', None) or getattr(obj, 'created_at', None) or timezone.make_aware(
                timezone.datetime(1970, 1, 1)
            )

        records = [('normal', item, sort_time(item)) for item in normal]
        records.extend(('recommended', item, sort_time(item)) for item in recommended)
        records.sort(key=lambda item: item[2], reverse=True)

        seen = set()
        unique_records = []
        for kind, item, timestamp in records:
            identity = item.unique_id if kind == 'recommended' else item.pk
            key = (kind, str(identity))
            if key in seen:
                continue
            seen.add(key)
            unique_records.append((kind, item, timestamp))

        page_records = unique_records[(page - 1) * page_size:end]
        normal_page = [item for kind, item, _ in page_records if kind == 'normal']
        recommended_page = [item for kind, item, _ in page_records if kind == 'recommended']
        normal_data = {
            item.pk: data for item, data in zip(
                normal_page,
                SimplePlaylistSerializer(normal_page, many=True, context={'request': request}).data,
            )
        }
        recommended_data = {
            item.unique_id: data for item, data in zip(
                recommended_page,
                PlaylistSummarySerializer(recommended_page, many=True, context={'request': request}).data,
            )
        }
        page_items = [
            normal_data[item.pk] if kind == 'normal' else recommended_data[item.unique_id]
            for kind, item, _ in page_records
        ]

        total = normal_total + recommended_total + len(generated)
        has_next = total > end
        next_url = None
        if has_next:
            params = request.query_params.copy()
            params['page'] = page + 1
            params['page_size'] = page_size
            next_url = absolute_api_url(request, request.path) + '?' + params.urlencode()

        profile_data['user_playlists'] = {
            'count': len(page_items),
            'total': total,
            'next': next_url,
            'results': page_items,
        }
        return Response(profile_data)



def _home_song_queryset(require_preview=False):
    qs = _song_card_queryset()
    if require_preview:
        qs = qs.filter(preview_audio_url__isnull=False).exclude(preview_audio_url='')
    return qs


def _home_album_queryset():
    song_qs = _home_song_queryset().order_by('-release_date', '-created_at')
    return Album.objects.select_related('artist').prefetch_related(
        'genres', 'sub_genres', 'moods', Prefetch('songs', queryset=song_qs, to_attr='_card_songs')
    )


def _home_artist_queryset():
    return Artist.objects.prefetch_related(
        Prefetch('social_account_links', queryset=ArtistSocialAccount.objects.select_related('platform'), to_attr='_social_links')
    )



def _artist_popularity_queryset():
    plays = Song.objects.filter(
        artist_id=OuterRef('pk'), status=Song.STATUS_PUBLISHED
    ).values('artist_id').annotate(total=Sum('plays')).values('total')[:1]
    likes = SongLike.objects.filter(song__artist_id=OuterRef('pk')).values(
        'song__artist_id'
    ).annotate(total=Count('id')).values('total')[:1]
    additions = UserPlaylist.songs.through.objects.filter(song__artist_id=OuterRef('pk')).values(
        'song__artist_id'
    ).annotate(total=Count('id')).values('total')[:1]
    return _home_artist_queryset().annotate(
        total_plays=Coalesce(Subquery(plays, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField()),
        total_likes=Coalesce(Subquery(likes, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField()),
        total_playlist_adds=Coalesce(Subquery(additions, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField()),
    ).annotate(score=F('total_plays') + F('total_likes') + F('total_playlist_adds'))


def _album_popularity_queryset():
    plays = Song.objects.filter(
        album_id=OuterRef('pk'), status=Song.STATUS_PUBLISHED
    ).values('album_id').annotate(total=Sum('plays')).values('total')[:1]
    song_likes = SongLike.objects.filter(song__album_id=OuterRef('pk')).values(
        'song__album_id'
    ).annotate(total=Count('id')).values('total')[:1]
    album_likes = AlbumLike.objects.filter(album_id=OuterRef('pk')).values(
        'album_id'
    ).annotate(total=Count('id')).values('total')[:1]
    additions = UserPlaylist.songs.through.objects.filter(song__album_id=OuterRef('pk')).values(
        'song__album_id'
    ).annotate(total=Count('id')).values('total')[:1]
    return _home_album_queryset().exclude(title__iexact='single').annotate(
        total_song_plays=Coalesce(Subquery(plays, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField()),
        total_song_likes=Coalesce(Subquery(song_likes, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField()),
        album_likes=Coalesce(Subquery(album_likes, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField()),
        total_playlist_adds=Coalesce(Subquery(additions, output_field=BigIntegerField()), Value(0), output_field=BigIntegerField()),
    ).annotate(score=F('total_song_plays') + F('total_song_likes') + F('album_likes') + F('total_playlist_adds'))


def _home_playlist_queryset(user=None):
    authenticated = bool(user is not None and getattr(user, 'is_authenticated', False))
    audience = Q(user__isnull=True)
    if authenticated:
        audience |= Q(user=user)
    song_filter = Q(songs__status=Song.STATUS_PUBLISHED)
    if not authenticated:
        song_filter &= Q(songs__preview_audio_url__isnull=False) & ~Q(songs__preview_audio_url='')
    song_qs = _home_song_queryset(require_preview=not authenticated).order_by('-release_date', '-created_at')
    return RecommendedPlaylist.objects.filter(audience).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())
    ).select_related('playlist_ref').annotate(
        songs_count_value=Count('songs', filter=song_filter, distinct=True),
        likes_count_value=Count('liked_by', distinct=True),
    ).filter(songs_count_value__gt=0).prefetch_related(
        Prefetch('songs', queryset=song_qs, to_attr='_card_songs')
    )


def _time_bucket(minutes=20, value=None):
    moment = value or timezone.now()
    return int(moment.timestamp() // (minutes * 60))


def _recent_play_song_ids(require_preview=False, days=1, limit=300):
    """Return ranked song IDs from real plays, refreshed every two minutes."""
    bucket = _time_bucket(2)
    key = stable_cache_key(
        'recent-play-song-ids', days, require_preview, limit, bucket,
        cache_version(CATALOG_VERSION_KEY), 'v2',
    )
    cached = cache_get(key)
    if cached is not None:
        return cached

    links = Song.play_counts.through.objects.filter(
        playcount__created_at__gte=timezone.now() - timedelta(days=days),
        song__status=Song.STATUS_PUBLISHED,
    )
    if require_preview:
        links = links.filter(song__preview_audio_url__isnull=False).exclude(
            song__preview_audio_url=''
        )
    rows = links.values('song_id').annotate(
        total=Count('playcount_id')
    ).order_by('-total', '-song_id')[:limit]
    ids = [row['song_id'] for row in rows]
    cache_set(key, ids, 120)
    return ids


def _guest_daily_song_ids(limit=48):
    """24-hour guest chart with a popularity fallback and no duplicate IDs."""
    ranked = _recent_play_song_ids(require_preview=True, days=1, limit=max(limit * 4, 120))
    ids = list(ranked[:limit])
    if len(ids) < limit:
        fallback = _cached_ranked_ids(
            'guest-daily-popular-fallback',
            _home_song_queryset(True).order_by('-plays', '-release_date', '-created_at'),
            max(limit * 4, 120), 180, 'v2',
        )
        seen = set(ids)
        ids.extend(song_id for song_id in fallback if song_id not in seen)
    return ids[:limit]


def _pick_ids(queryset, size, seed, used=None, pool_size=100):
    candidates = list(queryset.values_list('id', flat=True)[:pool_size])
    random.Random(str(seed)).shuffle(candidates)
    if used is None:
        used = set()
    picked = [song_id for song_id in candidates if song_id not in used][:size]
    if len(picked) < size:
        picked.extend([
            song_id for song_id in candidates if song_id not in picked
        ][:size - len(picked)])
    used.update(picked)
    return picked


def _rotate_ranked_ids(ids, size, seed, anchor=5):
    """Keep the strongest items visible while rotating the rest deterministically."""
    ranked = list(dict.fromkeys(ids))
    fixed = ranked[:min(anchor, size)]
    pool = ranked[len(fixed):]
    random.Random(str(seed)).shuffle(pool)
    return (fixed + pool)[:size]


def _dynamic_playlist_recipes(require_preview=False, bucket=None):
    """Cache lightweight recipes; hydrate current Song rows at response time."""
    bucket = bucket or _time_bucket(15)
    version = cache_version(CATALOG_VERSION_KEY)
    key = stable_cache_key('fresh-playlist-recipes', require_preview, bucket, version, 'v6')
    cached, claimed = cache_get_or_claim(key)
    if cached is not None:
        return cached

    base = _home_song_queryset(require_preview)
    used = set()
    recipes = []

    def add(code, title, title_en, description, description_en, playlist_type, ids):
        if len(ids) < 3:
            return
        recipes.append({
            'code': code,
            'title': title,
            'title_en': title_en,
            'description': description,
            'description_en': description_en,
            'playlist_type': playlist_type,
            'song_ids': ids,
        })

    trending_pool = _recent_play_song_ids(require_preview=require_preview, days=1, limit=120)
    if trending_pool:
        trend_ids = _rotate_ranked_ids(
            trending_pool[:60], 18, f'trending:{bucket}', anchor=6,
        )
        used.update(trend_ids)
    else:
        trend_ids = _pick_ids(
            base.order_by('-plays', '-release_date', '-created_at'), 18,
            f'trending:{bucket}', used,
        )
    add(
        'now', 'داغِ همین حالا', 'Trending Right Now',
        'پرشنونده‌ترین انتخاب‌های ۲۴ ساعت گذشته', 'Most-played picks from the last 24 hours',
        RecommendedPlaylist.PLAYLIST_TYPE_SIMILAR_TASTE, trend_ids,
    )

    add(
        'fresh', 'تازه رسیده‌ها', 'Fresh Arrivals',
        'ریلیزهای تازه با چیدمانی که مرتب نو می‌شود', 'Fresh releases in a regularly refreshed mix',
        RecommendedPlaylist.PLAYLIST_TYPE_DISCOVER_GENRE,
        _pick_ids(base.order_by('-release_date', '-created_at', '-plays'), 18, f'fresh:{bucket}', used),
    )
    add(
        'popular', 'محبوب‌های صداباکس', 'SedaBox Favorites',
        'ترک‌های امتحان‌پس‌داده برای یک پخش بی‌وقفه', 'Proven favorites for uninterrupted listening',
        RecommendedPlaylist.PLAYLIST_TYPE_SIMILAR_TASTE,
        _pick_ids(base.order_by('-plays', '-release_date'), 18, f'popular:{bucket}', used),
    )

    genre_filter = Q(songs__status=Song.STATUS_PUBLISHED)
    mood_filter = Q(songs__status=Song.STATUS_PUBLISHED)
    if require_preview:
        genre_filter &= Q(songs__preview_audio_url__isnull=False) & ~Q(songs__preview_audio_url='')
        mood_filter &= Q(songs__preview_audio_url__isnull=False) & ~Q(songs__preview_audio_url='')
    genres = list(Genre.objects.filter(genre_filter).annotate(
        song_total=Count('songs', distinct=True)
    ).filter(song_total__gte=3).order_by('-song_total', 'name').values('id', 'name', 'name_en', 'slug')[:8])
    random.Random(f'genres:{bucket}').shuffle(genres)
    for index, genre in enumerate(genres[:2], 1):
        ids = _pick_ids(
            base.filter(genres__id=genre['id']).distinct().order_by('-plays', '-release_date'),
            16, f'genre:{genre["id"]}:{bucket}', used,
        )
        add(
            f'genre{index}', f'موج {genre["name"]}',
            f'{genre.get("name_en") or genre.get("slug", "").replace("-", " " ).title() or genre["name"]} Wave',
            f'یک میکس تازه از فضای {genre["name"]}',
            f'A fresh mix inspired by {genre.get("name_en") or genre.get("slug", "").replace("-", " " ).title() or genre["name"]}',
            RecommendedPlaylist.PLAYLIST_TYPE_DISCOVER_GENRE, ids,
        )

    moods = list(Mood.objects.filter(mood_filter).annotate(
        song_total=Count('songs', distinct=True)
    ).filter(song_total__gte=3).order_by('-song_total', 'name').values('id', 'name', 'name_en', 'slug')[:8])
    random.Random(f'moods:{bucket}').shuffle(moods)
    if moods:
        mood = moods[0]
        add(
            'mood', f'{mood["name"]} برای این لحظه',
            f'{mood.get("name_en") or mood.get("slug", "").replace("-", " " ).title() or mood["name"]} for This Moment',
            'یک جریان کوتاه و منسجم برای حال‌وهوای الآن',
            'A short, cohesive flow for your current mood',
            RecommendedPlaylist.PLAYLIST_TYPE_MOOD_BASED,
            _pick_ids(
                base.filter(moods__id=mood['id']).distinct().order_by('-plays', '-release_date'),
                16, f'mood:{mood["id"]}:{bucket}', used,
            ),
        )

    add(
        'discover', 'کشف‌های تازه', 'Fresh Discoveries',
        'کمتر تکراری، تازه‌تر و مناسب پیدا کردن صدای بعدی',
        'Less repetition, more freshness, and a new sound to discover',
        RecommendedPlaylist.PLAYLIST_TYPE_DISCOVER_GENRE,
        _pick_ids(base.order_by('-created_at', 'plays'), 18, f'discover:{bucket}', used),
    )

    if claimed:
        cache_set(key, recipes, 2 * 60 * 60)
    return recipes


def _dynamic_playlist_items(user=None, bucket=None):
    authenticated = bool(user is not None and getattr(user, 'is_authenticated', False))
    require_preview = not authenticated
    bucket = bucket or _time_bucket(15)
    recipes = _dynamic_playlist_recipes(require_preview, bucket)
    song_ids = {song_id for recipe in recipes for song_id in recipe['song_ids']}
    song_map = _home_song_queryset(require_preview).filter(id__in=song_ids).in_bulk()
    creator_uid = _sedabox_unique_id()

    items = []
    now = timezone.now()
    for index, recipe in enumerate(recipes, 1):
        songs = [song_map[song_id] for song_id in recipe['song_ids'] if song_id in song_map]
        if len(songs) < 3:
            continue
        item = RecommendedPlaylist(
            id=-(bucket * 100 + index),
            unique_id=f'freshmix_{bucket}_{recipe["code"]}',
            title=recipe['title'],
            title_en=recipe['title_en'],
            description=recipe['description'],
            description_en=recipe['description_en'],
            playlist_type=recipe['playlist_type'],
            song_order=[song.id for song in songs],
            relevance_score=110 - index,
            match_percentage=max(76, 98 - index * 3),
            views=0,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(minutes=40),
        )
        item._card_songs = songs
        item._detail_songs = songs
        item._songs_count = len(songs)
        item._likes_count = 0
        item._is_liked = False
        item._is_saved = False
        item._is_dynamic = True
        item._creator_unique_id = creator_uid
        items.append(item)
    return items


def _user_has_music_activity(user):
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    key = stable_cache_key('user-music-activity', user.pk, 'v1')
    cached = cache_get(key)
    if cached is not None:
        return bool(cached)
    active = (
        SongLike.objects.filter(user=user).exists()
        or PlayCount.objects.filter(user=user).exists()
        or UserPlaylist.objects.filter(user=user, songs__isnull=False).exists()
    )
    cache_set(key, active, 300 if active else 30)
    return active


def _playlist_recommendation_items(user=None, limit=80):
    authenticated = bool(user is not None and getattr(user, 'is_authenticated', False))
    base = _home_playlist_queryset(user)
    dynamic = _dynamic_playlist_items(user)
    if authenticated:
        personal = list(base.filter(user=user).order_by('-relevance_score', '-created_at')[:limit])
        global_items = list(base.filter(user__isnull=True).order_by('-relevance_score', '-created_at')[:limit])
        ordered = (
            personal + dynamic + global_items
            if personal and _user_has_music_activity(user)
            else dynamic + global_items + personal
        )
    else:
        global_items = list(base.order_by('-relevance_score', '-created_at')[:limit])
        ordered = dynamic + global_items

    seen = set()
    items = []
    for item in ordered:
        key = item.unique_id
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= limit:
            break
    return items


def _dynamic_playlist_by_unique_id(user, unique_id):
    match = re.fullmatch(r'freshmix_(\d+)_([a-z0-9]+)', unique_id or '')
    if not match:
        return None
    bucket = int(match.group(1))
    return next((item for item in _dynamic_playlist_items(user, bucket) if item.unique_id == unique_id), None)



def _cached_ranked_ids(name, queryset, limit=300, timeout=300, *parts):
    key = stable_cache_key(name, cache_version(CATALOG_VERSION_KEY), *parts)
    ids = cache_get(key)
    if ids is None:
        ids = list(queryset.values_list('id', flat=True)[:limit])
        cache_set(key, ids, timeout)
    return ids


def _ordered_queryset_items(queryset, ids):
    objects = queryset.filter(id__in=ids).in_bulk()
    return [objects[item_id] for item_id in ids if item_id in objects]


def _attach_recommended_metrics(items, user=None):
    items = list(items)
    ids = [item.id for item in items if item.id and item.id > 0]
    liked = saved = set()
    if ids and user is not None and getattr(user, 'is_authenticated', False):
        liked = set(RecommendedPlaylist.objects.filter(id__in=ids, liked_by=user).values_list('id', flat=True))
        saved = set(RecommendedPlaylist.objects.filter(id__in=ids, saved_by=user).values_list('id', flat=True))
    for item in items:
        item._songs_count = getattr(item, 'songs_count_value', len(getattr(item, '_card_songs', [])))
        item._likes_count = getattr(item, 'likes_count_value', 0)
        item._is_liked = item.id in liked
        item._is_saved = item.id in saved
    return items


def _rotate_sample(items, limit, seed):
    items = list(items)
    if len(items) <= limit:
        return items
    rng = random.Random(str(seed))
    rng.shuffle(items)
    return items[:limit]


def _next_url(request, page_param, page, has_next):
    if not has_next:
        return None
    params = request.query_params.copy()
    params[page_param] = page + 1
    return absolute_api_url(request, f"{request.path}?{params.urlencode()}")


def _slice_items(items, page, size):
    start = (page - 1) * size
    chunk = list(items[start:start + size + 1])
    return chunk[:size], len(chunk) > size


def _song_recommendations(request, limit=10):
    user = request.user
    require_preview = not user.is_authenticated
    if require_preview:
        ids = _guest_daily_song_ids(limit)
        song_map = _home_song_queryset(True).filter(id__in=ids).in_bulk()
        songs = [song_map[sid] for sid in ids if sid in song_map]
        hydrate_song_metrics(songs, None)
        return 'daily_trending', songs

    version = cache_version(CATALOG_VERSION_KEY)
    affinity = cache_version(AFFINITY_VERSION_KEY)
    audience = f'user:{user.id}:{affinity}' if user.is_authenticated else 'guest'
    key = stable_cache_key('home-song-recommendations', audience, version, limit, 'v4')
    ids = cache_get(key)
    recommendation_type = 'personalized' if user.is_authenticated else 'guest_discovery'

    if not ids:
        base = _home_song_queryset(require_preview=require_preview)
        excluded = set()
        candidates = base
        if user.is_authenticated:
            liked = set(SongLike.objects.filter(user=user).values_list('song_id', flat=True))
            played = set(PlayCount.objects.filter(user=user).values_list('songs__id', flat=True))
            playlist = set(UserPlaylist.objects.filter(user=user).values_list('songs__id', flat=True))
            excluded = liked | played | playlist
            interacted = Song.objects.filter(id__in=excluded)
            genre_ids = list(interacted.exclude(genres__id=None).values('genres__id').annotate(n=Count('id')).order_by('-n').values_list('genres__id', flat=True)[:4])
            mood_ids = list(interacted.exclude(moods__id=None).values('moods__id').annotate(n=Count('id')).order_by('-n').values_list('moods__id', flat=True)[:3])
            artist_ids = list(interacted.values('artist_id').annotate(n=Count('id')).order_by('-n').values_list('artist_id', flat=True)[:4])
            if genre_ids or mood_ids or artist_ids:
                candidates = base.exclude(id__in=excluded).filter(
                    Q(genres__id__in=genre_ids) | Q(moods__id__in=mood_ids) | Q(artist_id__in=artist_ids)
                ).distinct()
            else:
                recommendation_type = 'trending'
        pool = list(candidates.order_by('-plays', '-release_date', '-created_at')[:120])
        if len(pool) < limit:
            seen = excluded | {song.id for song in pool}
            pool.extend(base.exclude(id__in=seen).order_by('-plays', '-release_date')[:limit - len(pool)])
        songs = _rotate_sample(pool, limit, f'{audience}:{timezone.now():%Y-%m-%d-%H}')
        ids = [song.id for song in songs]
        cache_set(key, ids, getattr(settings, 'CACHE_TTL_USER_HOME', 30) if user.is_authenticated else getattr(settings, 'CACHE_TTL_HOME', 90))
    song_map = _home_song_queryset(require_preview=require_preview).filter(id__in=ids).in_bulk()
    songs = [song_map[sid] for sid in ids if sid in song_map]
    hydrate_song_metrics(songs, user if user.is_authenticated else None)
    return recommendation_type, songs


def _paginated_payload(request, items, page_param, page, size, serializer):
    page_items, has_next = _slice_items(items, page, size)
    return {
        'count': len(page_items),
        'next': _next_url(request, page_param, page, has_next),
        'previous': None,
        'results': serializer(page_items, many=True, context={'request': request}).data,
    }


@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class HomeSummaryView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        user = request.user
        version = cache_version(CATALOG_VERSION_KEY)
        audience = f'user:{user.id}:{cache_version(AFFINITY_VERSION_KEY)}' if user.is_authenticated else 'guest'
        pages = {name: max(1, int(request.query_params.get(param, 1) or 1)) for name, param in {
            'rec': 'sr_page', 'latest': 'lr_page', 'artists': 'pa_page', 'albums': 'pal_page',
            'playlists': 'pr_page', 'discoveries': 'ds_page',
        }.items()}
        cache_key = stable_cache_key('home-summary', get_request_language(request), audience, version, pages, 'v11')
        cached, claimed = cache_get_or_claim(cache_key) if not user.is_authenticated else (None, False)
        if cached is not None:
            return Response(cached)

        rec_size = 12 if not user.is_authenticated else 6
        rec_type, rec_songs = _song_recommendations(request, 48 if not user.is_authenticated else 30)
        rec_page, rec_next = _slice_items(rec_songs, pages['rec'], rec_size)

        latest_qs = _home_song_queryset(not user.is_authenticated)
        latest_ids = _cached_ranked_ids(
            'home-latest', latest_qs.order_by('-release_date', '-created_at'), 80, 180,
            not user.is_authenticated,
        )
        latest = _ordered_queryset_items(latest_qs, latest_ids)
        latest_page, latest_next = _slice_items(latest, pages['latest'], 6)
        hydrate_song_metrics(latest_page, user if user.is_authenticated else None)

        artist_qs = _artist_popularity_queryset()
        artist_ids = _cached_ranked_ids('home-popular-artists', artist_qs.order_by('-score', '-verified', 'name'), 80, 300)
        artists = _ordered_queryset_items(artist_qs, artist_ids)
        artist_page, artist_next = _slice_items(artists, pages['artists'], 6)
        hydrate_artist_metrics(artist_page, user if user.is_authenticated else None)

        album_qs = _album_popularity_queryset()
        album_ids = _cached_ranked_ids('home-popular-albums', album_qs.order_by('-score', '-release_date'), 80, 300)
        albums = _ordered_queryset_items(album_qs, album_ids)
        album_page, album_next = _slice_items(albums, pages['albums'], 6)
        hydrate_album_metrics(album_page, user if user.is_authenticated else None)

        playlists = _playlist_recommendation_items(user, 80)
        playlist_page, playlist_next = _slice_items(playlists, pages['playlists'], 6)
        _attach_recommended_metrics(playlist_page, user)

        discovery_base = _home_song_queryset(not user.is_authenticated)
        excluded = {song.id for song in latest[:30]} | {song.id for song in rec_songs}
        discovery_pool = list(discovery_base.exclude(id__in=excluded).order_by('-created_at')[:120])
        discoveries = _rotate_sample(discovery_pool, 60, f'{audience}:{timezone.now():%Y-%m-%d-%H}')
        discovery_page, discovery_next = _slice_items(discoveries, pages['discoveries'], 6)
        hydrate_song_metrics(discovery_page, user if user.is_authenticated else None)

        payload = {
            'sections': 6,
            'songs_recommendations': {
                'type': rec_type, 'count': len(rec_page),
                'next': _next_url(request, 'sr_page', pages['rec'], rec_next),
                'message': (
                    'Most-played tracks from the last 24 hours, supplemented with popular picks'
                    if get_request_language(request) == 'en' else
                    'پرشنونده‌ترین‌های ۲۴ ساعت گذشته؛ با جایگزین محبوب‌ها اگر داده تازه کم باشد'
                ) if not user.is_authenticated else '',
                'message_fa': 'پرشنونده‌ترین‌های ۲۴ ساعت گذشته؛ با جایگزین محبوب‌ها اگر داده تازه کم باشد' if not user.is_authenticated else '',
                'message_en': 'Most-played tracks from the last 24 hours, supplemented with popular picks' if not user.is_authenticated else '',
                'songs': SongStreamSerializer(rec_page, many=True, context={'request': request}).data,
            },
            'latest_releases': {
                'count': len(latest_page), 'next': _next_url(request, 'lr_page', pages['latest'], latest_next),
                'results': SongSummarySerializer(latest_page, many=True, context={'request': request}).data,
            },
            'popular_artists': {
                'count': len(artist_page), 'next': _next_url(request, 'pa_page', pages['artists'], artist_next),
                'results': ArtistSummarySerializer(artist_page, many=True, context={'request': request}).data,
            },
            'popular_albums': {
                'count': len(album_page), 'next': _next_url(request, 'pal_page', pages['albums'], album_next),
                'results': AlbumSummarySerializer(album_page, many=True, context={'request': request}).data,
            },
            'playlist_recommendations': {
                'count': len(playlist_page), 'next': _next_url(request, 'pr_page', pages['playlists'], playlist_next),
                'results': PlaylistSummarySerializer(playlist_page, many=True, context={'request': request}).data,
            },
            'discoveries': {
                'count': len(discovery_page), 'next': _next_url(request, 'ds_page', pages['discoveries'], discovery_next),
                'results': SongSummarySerializer(discovery_page, many=True, context={'request': request}).data,
            },
        }
        if claimed and not user.is_authenticated:
            cache_set(cache_key, payload, getattr(settings, 'CACHE_TTL_HOME', 90))
        return Response(payload)



class UserRecommendationView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        _, size = _page_values(request, 20 if not request.user.is_authenticated else 10, 50)
        recommendation_type, songs = _song_recommendations(request, size)
        return Response({
            'type': recommendation_type,
            'message': (
                'Top picks from the last 24 hours'
                if get_request_language(request) == 'en' else 'منتخب‌های ۲۴ ساعت گذشته'
            ) if not request.user.is_authenticated else '',
            'message_fa': 'منتخب‌های ۲۴ ساعت گذشته' if not request.user.is_authenticated else '',
            'message_en': 'Top picks from the last 24 hours' if not request.user.is_authenticated else '',
            'songs': SongStreamSerializer(songs, many=True, context={'request': request}).data,
        })



class DiscoveriesView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        page, size = _page_values(request, 20, 50)
        user = request.user
        version = cache_version(CATALOG_VERSION_KEY)
        audience = f'user:{user.id}:{cache_version(AFFINITY_VERSION_KEY)}' if user.is_authenticated else 'guest'
        key = stable_cache_key('discoveries', audience, version, timezone.now().strftime('%Y-%m-%d-%H'), 'v4')
        ids = cache_get(key)
        if not ids:
            qs = _home_song_queryset(not user.is_authenticated)
            excluded = set()
            if user.is_authenticated:
                excluded |= set(SongLike.objects.filter(user=user).values_list('song_id', flat=True))
                excluded |= set(PlayCount.objects.filter(user=user).values_list('songs__id', flat=True))
            pool = list(qs.exclude(id__in=excluded).order_by('-created_at')[:240])
            if not pool:
                pool = list(qs.order_by('-created_at')[:240])
            pool = _rotate_sample(pool, len(pool), key)
            ids = [song.id for song in pool]
            cache_set(key, ids, getattr(settings, 'CACHE_TTL_DISCOVERY', 300))
        page_ids, has_next = _slice_items(ids, page, size)
        song_map = _home_song_queryset(not user.is_authenticated).filter(id__in=page_ids).in_bulk()
        songs = [song_map[sid] for sid in page_ids if sid in song_map]
        hydrate_song_metrics(songs, user if user.is_authenticated else None)
        serializer = SongSummarySerializer if request.query_params.get('summary') == 'true' else SongStreamSerializer
        return Response({
            'count': len(songs), 'next': _next_url(request, 'page', page, has_next), 'previous': None,
            'results': serializer(songs, many=True, context={'request': request}).data,
        })




@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class LatestReleasesView(generics.ListAPIView):
    permission_classes = [AllowAny]
    pagination_class = StandardResultsSetPagination
    serializer_class = SongStreamSerializer

    def get_queryset(self):
        return _home_song_queryset(not self.request.user.is_authenticated).order_by('-release_date', '-created_at')

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.get_queryset())
        items = list(page if page is not None else self.get_queryset())
        hydrate_song_metrics(items, request.user if request.user.is_authenticated else None)
        data = self.get_serializer(items, many=True).data
        return self.get_paginated_response(data) if page is not None else Response(data)



@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class PopularArtistsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        page, size = _page_values(request, 20, 100)
        queryset = _artist_popularity_queryset()
        ids = _cached_ranked_ids('popular-artists', queryset.order_by('-score', '-verified'), 500, 300)
        page_ids, has_next = _slice_items(ids, page, size)
        items = _ordered_queryset_items(queryset, page_ids)
        hydrate_artist_metrics(items, request.user if request.user.is_authenticated else None)
        return Response({
            'count': len(ids), 'next': _next_url(request, 'page', page, has_next), 'previous': None,
            'results': PopularArtistSerializer(items, many=True, context={'request': request}).data,
        })




@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class PopularAlbumsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        page, size = _page_values(request, 20, 100)
        queryset = _album_popularity_queryset()
        ids = _cached_ranked_ids('popular-albums', queryset.order_by('-score', '-release_date'), 500, 300)
        page_ids, has_next = _slice_items(ids, page, size)
        items = _ordered_queryset_items(queryset, page_ids)
        hydrate_album_metrics(items, request.user if request.user.is_authenticated else None)
        return Response({
            'count': len(ids), 'next': _next_url(request, 'page', page, has_next), 'previous': None,
            'results': PopularAlbumSerializer(items, many=True, context={'request': request}).data,
        })




@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class _GlobalChartView(APIView):
    permission_classes = [AllowAny]
    entity = 'song'
    days = 1

    def get(self, request):
        page, size = _page_values(request, 20, 100)
        cutoff = timezone.now() - timedelta(days=self.days)
        version = cache_version(CATALOG_VERSION_KEY)
        key = stable_cache_key('global-chart', self.entity, self.days, version, timezone.now().strftime('%Y-%m-%d-%H'), 'v3')
        ids = cache_get(key)
        if not ids:
            through = Song.play_counts.through
            links = through.objects.filter(playcount__created_at__gte=cutoff)
            if self.entity == 'song':
                rows = links.values('song_id').annotate(total=Count('playcount_id')).order_by('-total')[:300]
                ids = [row['song_id'] for row in rows]
            elif self.entity == 'artist':
                rows = links.values('song__artist_id').annotate(total=Count('playcount_id')).order_by('-total')[:300]
                ids = [row['song__artist_id'] for row in rows]
            else:
                rows = links.exclude(song__album_id=None).values('song__album_id').annotate(total=Count('playcount_id')).order_by('-total')[:300]
                ids = [row['song__album_id'] for row in rows]
            cache_set(key, ids, 300)
        page_ids, has_next = _slice_items(ids, page, size)
        if self.entity == 'song':
            objects = _home_song_queryset(not request.user.is_authenticated).filter(id__in=page_ids).in_bulk()
            items = [objects[x] for x in page_ids if x in objects]
            hydrate_song_metrics(items, request.user if request.user.is_authenticated else None)
            serializer = SongStreamSerializer
        elif self.entity == 'artist':
            objects = _home_artist_queryset().filter(id__in=page_ids).in_bulk()
            items = [objects[x] for x in page_ids if x in objects]
            hydrate_artist_metrics(items, request.user if request.user.is_authenticated else None)
            serializer = ArtistSummarySerializer
        else:
            objects = _home_album_queryset().filter(id__in=page_ids).in_bulk()
            items = [objects[x] for x in page_ids if x in objects]
            hydrate_album_metrics(items, request.user if request.user.is_authenticated else None)
            serializer = AlbumSummarySerializer
        return Response({'count': len(items), 'next': _next_url(request, 'page', page, has_next), 'previous': None,
                         'results': serializer(items, many=True, context={'request': request}).data})

class DailyTopSongsView(_GlobalChartView):
    entity = 'song'
    days = 1



@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class DailyTopArtistsView(_GlobalChartView):
    entity = 'artist'
    days = 1



@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class DailyTopAlbumsView(_GlobalChartView):
    entity = 'album'
    days = 1



@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class WeeklyTopSongsView(_GlobalChartView):
    entity = 'song'
    days = 7



@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class WeeklyTopArtistsView(_GlobalChartView):
    entity = 'artist'
    days = 7



@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class WeeklyTopAlbumsView(_GlobalChartView):
    entity = 'album'
    days = 7



@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class PlaylistRecommendationsView(generics.ListAPIView):
    permission_classes = [AllowAny]
    pagination_class = StandardResultsSetPagination
    serializer_class = RecommendedPlaylistListSerializer

    def _ensure_personal(self, user):
        key = stable_cache_key('ensure-playlist-recs', user.id, cache_version(AFFINITY_VERSION_KEY), 'v3')
        if cache_get(key) is not None or RecommendedPlaylist.objects.filter(user=user, expires_at__gt=timezone.now()).exists():
            return
        if not _user_has_music_activity(user):
            cache_set(key, True, 30)
            return
        interacted = Song.objects.filter(
            Q(liked_by=user) | Q(play_counts__user=user) | Q(user_playlists__user=user),
            status=Song.STATUS_PUBLISHED,
        ).distinct()
        genre_ids = list(interacted.exclude(genres__id=None).values('genres__id').annotate(n=Count('id')).order_by('-n').values_list('genres__id', flat=True)[:3])
        mood_ids = list(interacted.exclude(moods__id=None).values('moods__id').annotate(n=Count('id')).order_by('-n').values_list('moods__id', flat=True)[:2])
        configs = [('genre', gid) for gid in genre_ids] + [('mood', mid) for mid in mood_ids]
        for index, (kind, value) in enumerate(configs[:5], 1):
            filter_key = {'genres__id': value} if kind == 'genre' else {'moods__id': value}
            songs = list(_home_song_queryset().filter(**filter_key).order_by('-plays', '-release_date')[:20])
            if not songs:
                continue
            label_row = (
                Genre.objects.filter(id=value).values('name', 'name_en', 'slug').first()
                if kind == 'genre'
                else Mood.objects.filter(id=value).values('name', 'name_en', 'slug').first()
            ) or {}
            label = label_row.get('name') or 'برای شما'
            label_en = label_row.get('name_en') or (label_row.get('slug') or '').replace('-', ' ').title() or 'For You'
            playlist, _ = RecommendedPlaylist.objects.update_or_create(
                unique_id=f'smart_rec_{user.id}_{index}', defaults={
                    'user': user, 'title': label, 'title_en': label_en,
                    'description': 'پیشنهاد تازه براساس سلیقه و شنیده‌های شما',
                    'description_en': 'A fresh recommendation based on your taste and listening history',
                    'playlist_type': RecommendedPlaylist.PLAYLIST_TYPE_DISCOVER_GENRE if kind == 'genre' else RecommendedPlaylist.PLAYLIST_TYPE_MOOD_BASED,
                    'song_order': [song.id for song in songs], 'relevance_score': 100 - index,
                    'match_percentage': 95 - index, 'expires_at': timezone.now() + timedelta(days=2),
                })
            playlist.songs.set(songs)
        cache_set(key, True, 900)

    def get_queryset(self):
        if self.request.user.is_authenticated:
            self._ensure_personal(self.request.user)
        return _home_playlist_queryset(self.request.user).order_by('-relevance_score', '-created_at')

    def list(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            self._ensure_personal(request.user)
        all_items = _playlist_recommendation_items(request.user, 80)
        page = self.paginate_queryset(all_items)
        items = list(page if page is not None else all_items)
        _attach_recommended_metrics(items, request.user)
        data = self.get_serializer(items, many=True).data
        return self.get_paginated_response(data) if page is not None else Response(data)



@extend_schema(tags=['Home Page Endpoints اندپوینت های صفحه اصلی'])
class PlaylistRecommendationDetailView(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = RecommendedPlaylistDetailSerializer
    lookup_field = 'unique_id'

    def get_queryset(self):
        user = self.request.user
        authenticated = user.is_authenticated
        audience = Q(user__isnull=True)
        if authenticated:
            audience |= Q(user=user)
        song_filter = Q(songs__status=Song.STATUS_PUBLISHED)
        if not authenticated:
            song_filter &= Q(songs__preview_audio_url__isnull=False) & ~Q(songs__preview_audio_url='')
        song_qs = _home_song_queryset(require_preview=not authenticated).order_by('-release_date', '-created_at')
        return RecommendedPlaylist.objects.filter(audience).select_related('playlist_ref').annotate(
            songs_count_value=Count('songs', filter=song_filter, distinct=True),
            likes_count_value=Count('liked_by', distinct=True),
        ).filter(songs_count_value__gt=0).prefetch_related(
            Prefetch('songs', queryset=song_qs, to_attr='_detail_songs')
        )

    def retrieve(self, request, *args, **kwargs):
        unique_id = kwargs.get(self.lookup_field)
        instance = self.get_queryset().filter(unique_id=unique_id).first()
        if instance is None:
            instance = _dynamic_playlist_by_unique_id(request.user, unique_id)
        if instance is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if not getattr(instance, '_is_dynamic', False):
            RecommendedPlaylist.objects.filter(pk=instance.pk).update(views=F('views') + 1)
            instance.views += 1
            if request.user.is_authenticated:
                instance.viewed_by.add(request.user)
        songs = list(getattr(instance, '_detail_songs', []))
        hydrate_song_metrics(songs, request.user if request.user.is_authenticated else None)
        _attach_recommended_metrics([instance], request.user)
        return Response(self.get_serializer(instance).data)



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
        
        user = request.user
        
        # Check if already liked
        is_liked = playlist.liked_by.filter(id=user.id).exists()
        
        if is_liked:
            # Unlike: remove PlaylistLike if present, otherwise fall back to M2M
            from .models import PlaylistLike
            pl_like_qs = PlaylistLike.objects.filter(user=user, playlist_id=playlist.id)
            if pl_like_qs.exists():
                pl_like_qs.delete()
                return Response({'status': 'unliked', 'likes_count': PlaylistLike.objects.filter(playlist=playlist).count()})
            # fallback for RecommendedPlaylist M2M
            playlist.liked_by.remove(user)
            return Response({'status': 'unliked', 'likes_count': playlist.liked_by.count()})
        else:
            # Like
            if unique_id.startswith('smart_rec_'):
                # Freeze: Create a brand new persistent record for the user
                new_id = f"liked_rec_{user.id}_{uuid.uuid4().hex[:10]}"
                
                # Create the copy
                frozen_playlist = RecommendedPlaylist.objects.create(
                    unique_id=new_id,
                    user=user,
                    playlist_ref=playlist.playlist_ref,
                    title=playlist.title,
                    title_en=playlist.title_en,
                    description=playlist.description,
                    description_en=playlist.description_en,
                    playlist_type=playlist.playlist_type,
                    song_order=playlist.song_order,
                    relevance_score=playlist.relevance_score,
                    match_percentage=playlist.match_percentage,
                    expires_at=None # Persistent
                )
                
                # Copy songs (ManyToMany needs to be set after creation)
                frozen_playlist.songs.set(playlist.songs.all())
                
                # Add the user to liked_by of the NEW record (RecommendedPlaylist uses M2M)
                frozen_playlist.liked_by.add(user)

                # Also add the user to the original dynamic record's liked_by
                playlist.liked_by.add(user)

                return Response({
                    'status': 'liked',
                    'likes_count': frozen_playlist.liked_by.count(),
                    'new_unique_id': new_id,
                    'is_frozen': True
                })
            else:
                # Direct like for already persistent or other types
                playlist.liked_by.add(user)
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
    permission_classes = [AllowAny]
    TYPES = ('song', 'artist', 'album', 'playlist', 'user')

    def get(self, request):
        query = request.query_params.get('q', '').strip(); search_type = request.query_params.get('type') or None
        moods = sorted(request.query_params.getlist('moods')); page, page_size = _page_values(request, 20, 100)
        if search_type and search_type not in self.TYPES:
            return Response({'error': 'Invalid type. Must be song, artist, album, playlist, or user.'}, status=400)
        key = stable_cache_key('search-ids-v10', query.casefold(), search_type or 'mixed', moods, page, page_size, cache_version(CATALOG_VERSION_KEY), cache_version(USER_DIRECTORY_VERSION_KEY))
        cached, _ = cache_get_or_claim(key)
        if cached is None:
            if search_type:
                queryset = self._queryset(search_type, query, moods, request)
                offset=(page-1)*page_size; ids=list(queryset.values_list('id',flat=True)[offset:offset+page_size+1])
                cached={'refs':[(search_type,x) for x in ids[:page_size]],'has_next':len(ids)>page_size}
            else:
                per_type=max(1,(page_size+len(self.TYPES)-1)//len(self.TYPES)); type_offset=(page-1)*per_type
                groups=[]; has_next=False
                for kind in self.TYPES:
                    ids=list(self._queryset(kind,query,moods,request).values_list('id',flat=True)[type_offset:type_offset+per_type+1])
                    has_next = has_next or len(ids)>per_type; groups.append([(kind,x) for x in ids[:per_type]])
                refs=[]
                for index in range(max((len(group) for group in groups),default=0)):
                    for group in groups:
                        if index<len(group): refs.append(group[index])
                        if len(refs)>=page_size: break
                    if len(refs)>=page_size: break
                cached={'refs':refs,'has_next':has_next}
            cache_set(key,cached,getattr(settings,'CACHE_TTL_SEARCH',45))
        results=self._hydrate(cached['refs'],request)
        return Response({'results':SearchResultSerializer(results,many=True,context={'request':request}).data,
                         'page':page,'page_size':page_size,'has_next':cached['has_next'],'query':query,
                         'moods':moods,'type':search_type or 'mixed'})

    def _queryset(self, kind, query, moods, request):
        return {'song':self._search_songs,'artist':self._search_artists,'album':self._search_albums,
                'playlist':self._search_playlists,'user':self._search_users}[kind](query, moods, request)

    def _search_songs(self,q,moods,request):
        qs=Song.objects.filter(status=Song.STATUS_PUBLISHED)
        if q:
            clean=q.replace(' ','').replace('\u200c','')
            qs=qs.annotate(t_clean=Replace(Replace(Cast('title',TextField()),Value(' '),Value('')),Value('\u200c'),Value('')),
                           a_clean=Replace(Replace(Cast('artist__name',TextField()),Value(' '),Value('')),Value('\u200c'),Value('')))
            qs=qs.filter(Q(t_clean__icontains=clean)|Q(a_clean__icontains=clean)|Q(title__icontains=q)|Q(title_en__icontains=q)|
                         Q(artist__name__icontains=q)|Q(artist__name_en__icontains=q)|Q(album__title__icontains=q)|Q(album__title_en__icontains=q)|
                         Q(description__icontains=q)|Q(description_en__icontains=q)|Q(lyrics__icontains=q)|Q(lyrics_en__icontains=q)|
                         Q(label__icontains=q)|Q(label_en__icontains=q)|Q(producers__icontains=q)|Q(producers_en__icontains=q)|
                         Q(composers__icontains=q)|Q(composers_en__icontains=q)|Q(lyricists__icontains=q)|Q(lyricists_en__icontains=q)|
                         Q(featured_artists__name__icontains=q)|Q(featured_artists__name_en__icontains=q)|
                         Q(featured_artists__artistic_name__icontains=q)|Q(featured_artists__artistic_name_en__icontains=q))
        if moods:
            qs=qs.filter(Q(moods__id__in=moods) if all(x.isdigit() for x in moods) else Q(moods__slug__in=moods))
        return qs.distinct().order_by('-plays','-created_at')
    def _search_artists(self,q,moods,request):
        qs=Artist.objects.all()
        if q: qs=qs.filter(Q(name__icontains=q)|Q(name_en__icontains=q)|Q(artistic_name__icontains=q)|Q(artistic_name_en__icontains=q)|Q(bio__icontains=q)|Q(bio_en__icontains=q)|Q(unique_id__icontains=q))
        return qs.order_by('-verified','-created_at')
    def _search_albums(self,q,moods,request):
        qs=Album.objects.exclude(Q(title__iexact='single')|Q(title='سینگل'))
        if q: qs=qs.filter(Q(title__icontains=q)|Q(title_en__icontains=q)|Q(description__icontains=q)|Q(description_en__icontains=q)|Q(artist__name__icontains=q)|Q(artist__name_en__icontains=q))
        return qs.order_by('-release_date','-created_at')
    def _search_playlists(self,q,moods,request):
        qs=Playlist.objects.all()
        if q: qs=qs.filter(Q(title__icontains=q)|Q(title_en__icontains=q)|Q(description__icontains=q)|Q(description_en__icontains=q))
        if moods: qs=qs.filter(Q(moods__id__in=moods) if all(x.isdigit() for x in moods) else Q(moods__slug__in=moods))
        return qs.distinct().order_by('-created_at')
    def _search_users(self,q,moods,request):
        qs=User.objects.filter(is_active=True,is_banned=False,roles__contains=User.ROLE_AUDIENCE).exclude(Q(unique_id__isnull=True)|Q(unique_id=''))
        if request.user.is_authenticated: qs=qs.exclude(pk=request.user.pk)
        if q: qs=qs.filter(Q(unique_id__icontains=q)|Q(first_name__icontains=q)|Q(last_name__icontains=q))
        return qs.order_by('-date_joined')

    def _hydrate(self,refs,request):
        grouped={kind:[] for kind in self.TYPES}
        for kind,pk in refs: grouped[kind].append(pk)
        querysets={
            'song':_song_card_queryset().filter(pk__in=grouped['song']),
            'artist':Artist.objects.filter(pk__in=grouped['artist']),
            'album':Album.objects.select_related('artist').filter(pk__in=grouped['album']),
            'playlist':Playlist.objects.filter(pk__in=grouped['playlist']),
            'user':User.objects.select_related('image_profile').filter(pk__in=grouped['user']),
        }
        maps={kind:{obj.pk:obj for obj in qs} for kind,qs in querysets.items()}
        results=[maps[kind][pk] for kind,pk in refs if pk in maps[kind]]
        hydrate_song_metrics([x for x in results if isinstance(x,Song)],request.user,False)
        hydrate_album_metrics([x for x in results if isinstance(x,Album)],request.user)
        hydrate_playlist_metrics([x for x in results if isinstance(x,Playlist)],request.user)
        hydrate_artist_metrics([x for x in results if isinstance(x,Artist)],request.user)
        user_ids=[x.pk for x in results if isinstance(x,User)]
        followed=set(Follow.objects.filter(follower_user=request.user,followed_user_id__in=user_ids).values_list('followed_user_id',flat=True)) if request.user.is_authenticated and user_ids else set()
        for obj in results:
            if isinstance(obj,User): obj._is_following=obj.pk in followed
        return results


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
        # list view: return event playlists with lightweight playlist covers
        queryset = EventPlaylist.objects.all().prefetch_related(
            'playlists',
            'playlists__songs',
        )

        time_of_day = request.query_params.get('time_of_day')
        if time_of_day:
            queryset = queryset.filter(time_of_day=time_of_day)

        from .serializers import EventPlaylistListSerializer
        serializer = EventPlaylistListSerializer(queryset, many=True, context={'request': request})
        return Response(serializer.data)


@extend_schema(tags=['Search Page Endpoints اندپوینت های صفحه جستجو'])
class EventPlaylistDetailView(APIView):
    """Return a single EventPlaylist with playlists and summarized songs (SongSummarySerializer)."""
    permission_classes = [permissions.AllowAny]

    @extend_schema(
        summary="جزئیات پلی‌لیست مناسبتی",
        description="دریافت جزئیات یک گروه پلی‌لیست مناسبتی و لیست آهنگ‌ها (خلاصه شده).",
        responses={200: 'EventPlaylistDetailSerializer'}
    )
    def get(self, request, pk):
        from django.shortcuts import get_object_or_404
        queryset = EventPlaylist.objects.all().prefetch_related(
            'playlists',
            'playlists__songs',
            'playlists__genres',
            'playlists__moods',
            'playlists__tags',
        )
        obj = get_object_or_404(queryset, pk=pk)

        from .serializers import EventPlaylistDetailSerializer
        serializer = EventPlaylistDetailSerializer(obj, context={'request': request})
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

        # Create a plain dict for the serializer input to avoid QueryDict and pickling issues
        data = {}
        for key in request.data:
            val = request.data.get(key)
            if not hasattr(val, 'read'): # Skip file handles
                data[key] = val

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
            # Support comma-separated values, case-insensitive matching against allowed statuses
            raw = status_param
            parts = [p.strip() for p in raw.split(',') if p.strip()]
            allowed = {c[0] for c in Song.STATUS_CHOICES}
            valid = []
            for p in parts:
                if p in allowed:
                    valid.append(p)
                    continue
                pl = p.lower()
                for a in allowed:
                    if a.lower() == pl:
                        valid.append(a)
                        break

            if valid:
                queryset = queryset.filter(status__in=valid)
            else:
                # If no valid status tokens provided, return empty result set
                queryset = queryset.none()

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
        
        # Determine featured artist IDs and names for filename
        featured_ids = _normalize_id_list(request.data.getlist('featured_artist_ids') if hasattr(request.data, 'getlist') else request.data.get('featured_artist_ids', []))
        featured_names = []
        if featured_ids:
            featured_names = list(Artist.objects.filter(id__in=featured_ids).values_list('artistic_name', flat=True))
            # Fallback to name if artistic_name is empty
            if not any(featured_names):
                featured_names = list(Artist.objects.filter(id__in=featured_ids).values_list('name', flat=True))

        # Get audio info
        duration, bitrate, format_ext = get_audio_info(audio_file)
        if not format_ext:
            # Fallback to extension
            _, ext = os.path.splitext(audio_file.name)
            format_ext = ext.lstrip('.').lower()
        
        # Build filename base and sanitize
        if featured_names:
            filename_base = f"{artist_name} - {title} (feat. {', '.join(filter(None, featured_names))})"
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
            # Keep original name and format for cover image
            cover_url, _ = upload_file_to_r2(cover_image, folder='covers')

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
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids', 'featured_artist_ids']:
            if hasattr(request.data, 'getlist'):
                raw_val = request.data.getlist(field)
            else:
                raw_val = request.data.get(field)
            normalized = _normalize_id_list(raw_val)
            if normalized:
                # For featured_artists, the serializer has featured_artist_ids_write if needed
                # or we can rely on featured_artist_ids directly if the serializer supports it.
                # Based on AdminSongSerializer, we use featured_artist_ids_write.
                clean[f"{field}_write"] = normalized

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
            # Ensure the created Song gets linked to the artist instance
            serializer.save(artist=artist)
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
        
        # Create a plain dict for the serializer input to avoid QueryDict list-of-lists and pickling issues
        data = {}
        list_fields = ['genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids', 'featured_artist_ids', 
                       'producers', 'composers', 'lyricists', 
                       'genre_ids_write', 'sub_genre_ids_write', 'mood_ids_write', 'tag_ids_write', 'featured_artist_ids_write',
                       'genres', 'sub_genres', 'moods', 'tags']
        for key in request.data:
            if hasattr(request.data, 'getlist') and key in list_fields:
                data[key] = request.data.getlist(key)
            else:
                val = request.data.get(key)
                if not hasattr(val, 'read'): # Skip file handles
                    data[key] = val

        # Map user-friendly field names to serializer write_only fields
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids', 'featured_artist_ids']:
            if field in data and f"{field}_write" not in data:
                raw_val = data.get(field)
                normalized = _normalize_id_list(raw_val)
                if normalized is not None:
                    data[f"{field}_write"] = normalized
        
        audio_file = request.FILES.get('audio_file')
        if audio_file:
            title = data.get('title', song.title)
            artist_name = artist.artistic_name or artist.name
            
            # For filename, we prefer IDs if provided, else current relation
            featured_ids = data.get('featured_artist_ids_write')
            featured_names = []
            if featured_ids:
                featured_names = list(Artist.objects.filter(id__in=featured_ids).values_list('artistic_name', flat=True))
                # Fallback to name if artistic_name is empty
                if not any(featured_names):
                    featured_names = list(Artist.objects.filter(id__in=featured_ids).values_list('name', flat=True))
            else:
                featured_names = list(song.featured_artists.values_list('artistic_name', flat=True))
                if not any(featured_names):
                    featured_names = list(song.featured_artists.values_list('name', flat=True))
            
            featured_names = [n for n in featured_names if n]

            duration, bitrate, format_ext = get_audio_info(audio_file)
            if not format_ext:
                _, ext = os.path.splitext(audio_file.name)
                format_ext = ext.lstrip('.').lower()
            
            # Build filename base and sanitize
            if featured_names:
                filename_base = f"{artist_name} - {title} (feat. {', '.join(featured_names)})"
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
            # Keep original name and format for cover image
            cover_url, _ = upload_file_to_r2(cover_image, folder='covers')
            data['cover_image'] = cover_url

        serializer = SongSerializer(song, data=data, partial=partial, context={'request': request})
        if serializer.is_valid():
            serializer.save()
            return Response({
                "message": "OK",
                "song": serializer.data
            })
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk=None):
        """Delete a song record and try to remove related files from R2 (best-effort).

        Returns OK if the DB record is removed regardless of R2 deletion success.
        """
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        song = get_object_or_404(Song, pk=pk, artist=artist)

        # Collect possible file URLs from the song
        file_urls = []
        for field in ('audio_file', 'converted_audio_url', 'cover_image'):
            val = getattr(song, field, None)
            if val:
                file_urls.append(val)

        # Helper to extract object key from CDN/R2 URLs (similar to utils.generate_signed_r2_url)
        from urllib.parse import unquote
        cdn_base = getattr(settings, 'R2_CDN_BASE', 'https://cdn.sedabox.com').rstrip('/')

        client_kwargs = {
            'service_name': 's3',
            'endpoint_url': getattr(settings, 'R2_ENDPOINT_URL', None),
            'aws_access_key_id': getattr(settings, 'R2_ACCESS_KEY_ID', None),
            'aws_secret_access_key': getattr(settings, 'R2_SECRET_ACCESS_KEY', None),
            'config': Config(signature_version='s3v4'),
        }
        session_token = getattr(settings, 'R2_SESSION_TOKEN', None)
        if session_token:
            client_kwargs['aws_session_token'] = session_token
        client_kwargs = {k: v for k, v in client_kwargs.items() if v is not None}

        s3 = None
        tried_delete = []
        for url in file_urls:
            key = None
            try:
                if url.startswith(cdn_base):
                    key = unquote(url.replace(cdn_base + '/', ''))
                elif 'r2.cloudflarestorage.com' in url or 'r2.dev' in url:
                    parts = url.split('/')
                    if len(parts) > 3:
                        key = unquote('/'.join(parts[3:]))
                elif url.startswith('http'):
                    # External URL not in our R2; skip deletion
                    key = None

                if not key:
                    continue

                # Lazy-create client
                if s3 is None:
                    s3 = boto3.client(**client_kwargs)

                bucket = getattr(settings, 'R2_BUCKET_NAME')
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                    tried_delete.append(key)
                except Exception as e:
                    # Best-effort: log and continue
                    print(f"DEBUG: Failed to delete R2 object {key}: {e}")
            except Exception as e:
                print(f"DEBUG: Error while attempting to parse/delete URL {url}: {e}")

        # Delete DB record regardless of R2 deletion outcome
        try:
            song.delete()
        except Exception as e:
            return Response({"error": "Failed to delete song record", "detail": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        return Response({"message": "OK", "deleted_files": tried_delete})


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
        # Create a plain dict for the serializer input to avoid QueryDict issues and pickling errors
        album_data = {}
        list_fields = ['genre_ids', 'sub_genre_ids', 'mood_ids', 'genre_ids_write', 'sub_genre_ids_write', 'mood_ids_write']
        for key in request.data:
            if hasattr(request.data, 'getlist') and key in list_fields:
                album_data[key] = request.data.getlist(key)
            else:
                val = request.data.get(key)
                if not hasattr(val, 'read'): # Skip file handles
                    album_data[key] = val
        
        # Map user-friendly field names to serializer write_only fields for album
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids']:
            if field in album_data and f"{field}_write" not in album_data:
                raw_val = album_data.get(field)
                normalized = _normalize_id_list(raw_val)
                if normalized is not None:
                    album_data[f"{field}_write"] = normalized

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
            for list_field in ['producers', 'composers', 'lyricists']:
                val = request.data.getlist(f"{prefix}{list_field}")
                if val:
                    # drop empty entries coming from form serialization
                    song_data[list_field] = _clean_string_list(val)

            # Handle ManyToMany IDs
            for id_field in ['genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids', 'featured_artist_ids']:
                val = request.data.getlist(f"{prefix}{id_field}")
                if val:
                    # Use _write for consistency with SongSerializer expectation if configured
                    if id_field == 'featured_artist_ids':
                        song_data['featured_artist_ids'] = val
                    else:
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
        
        # Create a plain dict for the serializer input to avoid QueryDict issues and pickling errors
        album_data = {}
        list_fields = ['genre_ids', 'sub_genre_ids', 'mood_ids', 'genre_ids_write', 'sub_genre_ids_write', 'mood_ids_write']
        for key in request.data:
            if hasattr(request.data, 'getlist') and key in list_fields:
                album_data[key] = request.data.getlist(key)
            else:
                val = request.data.get(key)
                if not hasattr(val, 'read'): # Skip file handles
                    album_data[key] = val
        
        # Map user-friendly field names to serializer write_only fields for album
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids']:
            if field in album_data and f"{field}_write" not in album_data:
                raw_val = album_data.get(field)
                normalized = _normalize_id_list(raw_val)
                if normalized is not None:
                    album_data[f"{field}_write"] = normalized

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

        from django.db import transaction

        album = get_object_or_404(Album, pk=pk, artist=artist)

        # Delete songs belonging to this album and then delete the album itself atomically
        with transaction.atomic():
            songs_qs = Song.objects.filter(album=album)
            deleted_songs_count = songs_qs.count()
            songs_qs.delete()
            album.delete()

        return Response({
            "message": "Album and its songs deleted successfully",
            "deleted_songs": deleted_songs_count
        }, status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistAlbumSongsView(APIView):
    """
    Manage songs assigned to a specific album for the authenticated artist.

    POST: assign one or more existing songs (that belong to the artist) to the album.
    DELETE: remove one or more songs from the album (sets their album to null).
    """
    permission_classes = [IsAuthenticated]

    def get_artist(self, user):
        if User.ROLE_ARTIST not in user.roles:
            return None
        try:
            return user.artist_profile
        except Artist.DoesNotExist:
            return None

    @extend_schema(
        summary="اضافه یا اختصاص آهنگ‌ها به آلبوم",
        description="اختصاص لیستی از `song_ids` به آلبوم مشخص. فقط آهنگ‌های متعلق به این هنرمند پذیرفته می‌شوند.",
        request=inline_serializer(name='AssignSongsToAlbum', fields={
            'song_ids': serializers.ListField(child=serializers.IntegerField())
        }),
        responses={200: SongSerializer(many=True)}
    )
    def post(self, request, pk):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        album = get_object_or_404(Album, pk=pk, artist=artist)

        raw = request.data.get('song_ids') or request.data.get('song_id') or request.data.getlist('song_ids')
        song_ids = _normalize_id_list(raw)
        if not song_ids:
            return Response({'error': 'song_ids is required (list of integers)'}, status=status.HTTP_400_BAD_REQUEST)

        # Only update songs that belong to this artist
        qs = Song.objects.filter(id__in=song_ids, artist=artist)
        updated_count = qs.update(album=album)

        updated_ids = list(qs.values_list('id', flat=True))
        missing = [i for i in song_ids if i not in updated_ids]

        songs = Song.objects.filter(id__in=updated_ids)
        return Response({
            'updated_count': updated_count,
            'updated_ids': updated_ids,
            'missing_or_not_owned_ids': missing,
            'songs': SongSerializer(songs, many=True, context={'request': request}).data
        })

    @extend_schema(
        summary="حذف اختصاص آهنگ‌ها از آلبوم",
        description="حذف رابطهٔ آلبوم از روی یک یا چند آهنگ (تنها اگر آن آهنگ‌ها در این آلبوم باشند).",
        request=inline_serializer(name='RemoveSongsFromAlbum', fields={
            'song_ids': serializers.ListField(child=serializers.IntegerField())
        }),
        responses={200: inline_serializer(name='RemoveFromAlbumResponse', fields={'removed_count': serializers.IntegerField()})}
    )
    def delete(self, request, pk):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        album = get_object_or_404(Album, pk=pk, artist=artist)

        raw = request.data.get('song_ids') or request.data.get('song_id') or request.data.getlist('song_ids')
        song_ids = _normalize_id_list(raw)
        if not song_ids:
            return Response({'error': 'song_ids is required (list of integers)'}, status=status.HTTP_400_BAD_REQUEST)

        # Only remove album relation if the song currently belongs to this album and the artist matches
        qs = Song.objects.filter(id__in=song_ids, artist=artist, album=album)
        removed_count = qs.update(album=None)
        removed_ids = list(qs.values_list('id', flat=True))
        missing = [i for i in song_ids if i not in removed_ids]

        return Response({
            'removed_count': removed_count,
            'removed_ids': removed_ids,
            'missing_or_not_owned_or_not_in_album': missing
        })


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
                return Notification.objects.filter(artist=user.artist_profile, has_read=False).order_by('-created_at')
            return Notification.objects.none()

        return Notification.objects.filter(user=user, has_read=False).order_by('-created_at')

    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        notifications = list(queryset)
        
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


@extend_schema(
    summary="Get premium plan price",
    description="Returns the current Premium plan price and currency for audience clients (GET only).",
    responses={200: OpenApiTypes.OBJECT}
)
class PremiumPlanPriceView(APIView):
    """Public endpoint that returns the Premium plan price."""
    permission_classes = [AllowAny]

    def get(self, request):
        # Prefer the latest PlayConfiguration record's premium_plan_price.
        config = PlayConfiguration.objects.order_by('-updated_at').first()
        if config and config.premium_plan_price is not None:
            try:
                price_val = float(config.premium_plan_price)
            except Exception:
                price_val = float(getattr(settings, 'PREMIUM_PLAN_PRICE', 4.99))
        else:
            price_val = float(getattr(settings, 'PREMIUM_PLAN_PRICE', 4.99))

        currency = getattr(settings, 'PREMIUM_PLAN_CURRENCY', 'USD')

        return Response({
            'plan': 'premium',
            'price': price_val,
            
        }, status=status.HTTP_200_OK)