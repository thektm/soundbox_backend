from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status, permissions, serializers
from drf_spectacular.utils import extend_schema, OpenApiParameter, OpenApiTypes, inline_serializer
from rest_framework.pagination import PageNumberPagination
from django.shortcuts import get_object_or_404
from .models import (
    User, Artist, ArtistAuth, Song, Album, Genre, SubGenre, Mood, Tag, Report, 
    PlayConfiguration, BannerAd, AudioAd, PaymentTransaction, DepositRequest,
    SearchSection, EventPlaylist, Playlist, SupportTicket, SongPromotion,
    ArtistRelease, ArtistReleaseTrack, ArtistReleaseStatusHistory
)
from .models import PlayCount
from django.utils import timezone
from datetime import timedelta
from django.db import transaction
from django.db.models import Sum, Count, Q, Case, When, Value, IntegerField, F, Prefetch
from decimal import Decimal
from .admin_serializers import (
    AdminUserSerializer, AdminArtistSerializer, AdminArtistAuthSerializer, 
    AdminSongSerializer, AdminReportSerializer, AdminAlbumSerializer,
    AdminPlayConfigurationSerializer, AdminBannerAdSerializer, AdminAudioAdSerializer,
    AdminPaymentTransactionSerializer, AdminDepositRequestSerializer,
    AdminSearchSectionSerializer, AdminEventPlaylistSerializer, AdminPlaylistSerializer,
    AdminEmployeeSerializer, AdminSupportTicketSerializer, AdminSongPromotionSerializer
)
from rest_framework.parsers import MultiPartParser, FormParser
from .utils import upload_file_to_r2, convert_to_128kbps, get_audio_info, make_safe_filename, generate_signed_r2_url, check_r2_storage
from .song_play_metrics import apply_annotated_song_play_counts, hydrate_song_play_counts
from .performance import CATALOG_VERSION_KEY, cache_increment
import os

class AdminPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


def _admin_song_detail_queryset():
    return (
        Song.objects.select_related('artist', 'album')
        .exclude(status=Song.STATUS_DRAFT)
        .prefetch_related('featured_artists', 'genres', 'sub_genres', 'moods', 'tags')
        .annotate(likes_count=Count('liked_by', distinct=True))
    )


_CATALOG_VERSION_TTL = 7 * 24 * 60 * 60

def _bump_catalog_after_commit():
    transaction.on_commit(lambda: cache_increment(CATALOG_VERSION_KEY, _CATALOG_VERSION_TTL))

def _renumber_admin_release_tracks(release_ids):
    for release_id in set(release_ids):
        links = list(
            ArtistReleaseTrack.objects.select_for_update()
            .filter(release_id=release_id)
            .order_by('position', 'id')
        )
        # Move out of the constrained range first, then write the final order.
        for index, link in enumerate(links, start=1):
            if link.position != index:
                ArtistReleaseTrack.objects.filter(pk=link.pk).update(position=100000 + index)
        for index, link in enumerate(links, start=1):
            ArtistReleaseTrack.objects.filter(pk=link.pk).update(position=index)

def _take_down_empty_admin_releases(release_ids, actor, note):
    now = timezone.now()
    for release in ArtistRelease.objects.select_for_update().filter(pk__in=set(release_ids)):
        if release.release_tracks.exclude(song__status=Song.STATUS_DELETED).exists():
            continue
        if release.status in {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_TAKEN_DOWN}:
            continue
        previous = release.status
        release.status = ArtistRelease.STATUS_TAKEN_DOWN
        release.taken_down_at = now
        release.validation_snapshot = {}
        release.lock_version += 1
        release.save(update_fields=['status', 'taken_down_at', 'validation_snapshot', 'lock_version', 'updated_at'])
        ArtistReleaseStatusHistory.objects.create(
            release=release, from_status=previous, to_status=ArtistRelease.STATUS_TAKEN_DOWN,
            note=note, actor=actor,
        )

def _hard_delete_admin_song(song, actor):
    """Permanently remove a song while leaving release history internally coherent."""
    links = list(
        ArtistReleaseTrack.objects.select_for_update()
        .filter(song=song)
        .select_related('release')
    )
    release_ids = {link.release_id for link in links}
    if links:
        ArtistReleaseTrack.objects.filter(pk__in=[link.pk for link in links]).delete()
        _renumber_admin_release_tracks(release_ids)
        _take_down_empty_admin_releases(
            release_ids, actor, 'Song permanently deleted by an administrator.'
        )

    play_ids = list(song.play_counts.values_list('pk', flat=True))
    song.delete()
    if play_ids:
        PlayCount.objects.filter(pk__in=play_ids, songs__isnull=True).delete()



def _int_list(value):
    """Normalize repeated/CSV/JSON id inputs into a de-duplicated integer list."""
    if value is None:
        return []
    raw = value if isinstance(value, (list, tuple)) else str(value).split(',')
    result = []
    seen = set()
    for item in raw:
        try:
            item_id = int(item)
        except (TypeError, ValueError):
            continue
        if item_id > 0 and item_id not in seen:
            seen.add(item_id)
            result.append(item_id)
    return result


def _playlist_builder_queryset(params):
    """One source of truth for playlist discovery and smart-fill ranking."""
    source = str(params.get('source') or 'trend7').strip()
    recent_days = 7 if source == 'trend7' else 30
    since = timezone.now() - timedelta(days=recent_days)
    audio_score = Value(0, output_field=IntegerField())
    for field_name in AdminSongSerializer.AUDIO_CLASSIFICATION_FIELDS:
        audio_score = audio_score + Case(
            When(**{f'{field_name}__isnull': False}, then=Value(1)),
            default=Value(0), output_field=IntegerField(),
        )

    songs = (
        Song.objects.filter(status=Song.STATUS_PUBLISHED)
        .select_related('artist', 'album')
        .prefetch_related('featured_artists', 'genres', 'sub_genres', 'moods', 'tags')
        .annotate(
            likes_count=Count('liked_by', distinct=True),
            tracked_plays=Count('play_counts', distinct=True),
            recent_plays=Count('play_counts', filter=Q(play_counts__created_at__gte=since), distinct=True),
            genre_count=Count('genres', distinct=True),
            mood_count=Count('moods', distinct=True),
            audio_meta_count=audio_score,
        )
        .annotate(
            total_plays=F('plays') + F('tracked_plays'),
            metadata_sort_score=(
                Case(When(genre_count__gt=0, then=Value(7)), default=Value(0), output_field=IntegerField())
                + Case(When(mood_count__gt=0, then=Value(7)), default=Value(0), output_field=IntegerField())
                + F('audio_meta_count')
            )
        )
    )

    query = str(params.get('q') or '').strip()
    if query:
        songs = songs.filter(
            Q(title__icontains=query) | Q(title_en__icontains=query)
            | Q(artist__name__icontains=query) | Q(artist__name_en__icontains=query)
            | Q(artist__artistic_name__icontains=query) | Q(artist__artistic_name_en__icontains=query)
        )

    genre_ids = _int_list(params.get('genres'))
    mood_ids = _int_list(params.get('moods'))
    exclude_ids = _int_list(params.get('exclude'))
    if genre_ids:
        songs = songs.filter(genres__id__in=genre_ids)
    if mood_ids:
        songs = songs.filter(moods__id__in=mood_ids)
    if exclude_ids:
        songs = songs.exclude(id__in=exclude_ids)

    try:
        min_meta = max(0, min(100, int(params.get('min_meta') or 0)))
    except (TypeError, ValueError):
        min_meta = 0
    if min_meta:
        minimum_score = (min_meta * 21 + 99) // 100
        songs = songs.filter(metadata_sort_score__gte=minimum_score)

    ordering = {
        'trend7': ('-recent_plays', '-likes_count', '-total_plays', '-id'),
        'trend30': ('-recent_plays', '-likes_count', '-total_plays', '-id'),
        'plays': ('-total_plays', '-likes_count', '-id'),
        'likes': ('-likes_count', '-total_plays', '-id'),
        'newest': ('-created_at', '-id'),
        'metadata': ('-metadata_sort_score', '-total_plays', '-likes_count', '-id'),
    }.get(source, ('-recent_plays', '-likes_count', '-total_plays', '-id'))
    return songs.distinct().order_by(*ordering), source


def _playlist_builder_song_data(songs):
    items = list(songs)
    apply_annotated_song_play_counts(items)
    rows = list(AdminSongSerializer(items, many=True).data)
    for row, song in zip(rows, items):
        row['recent_plays'] = int(getattr(song, 'recent_plays', 0) or 0)
    return rows


def _playlist_builder_facets():
    genres = Genre.objects.annotate(
        song_count=Count('songs', filter=Q(songs__status=Song.STATUS_PUBLISHED), distinct=True)
    ).filter(song_count__gt=0).order_by('-song_count', 'name')
    moods = Mood.objects.annotate(
        song_count=Count('songs', filter=Q(songs__status=Song.STATUS_PUBLISHED), distinct=True)
    ).filter(song_count__gt=0).order_by('-song_count', 'name')
    return {
        'genres': [{'id': item.id, 'name': item.name, 'name_en': item.name_en, 'count': item.song_count} for item in genres],
        'moods': [{'id': item.id, 'name': item.name, 'name_en': item.name_en, 'count': item.song_count} for item in moods],
    }


def _set_playlist_song_order(playlist, songs):
    """Persist official playlist order using the existing implicit M2M row order."""
    through = Playlist.songs.through
    unique_songs = []
    seen = set()
    for song in songs:
        if song.pk not in seen:
            seen.add(song.pk)
            unique_songs.append(song)
    through.objects.filter(playlist_id=playlist.pk).delete()
    # Explicit inserts make the implicit through-row PK a deterministic order key.
    # Playlist edits are infrequent and capped, so correctness is preferred here.
    for song in unique_songs:
        through.objects.create(playlist_id=playlist.pk, song_id=song.pk)

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        role = str(request.query_params.get('role') or User.ROLE_AUDIENCE).strip()
        queryset = User.objects.filter(roles__contains=role).select_related('artist_profile')
        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(
                Q(phone_number__icontains=query) | Q(unique_id__icontains=query)
                | Q(first_name__icontains=query) | Q(last_name__icontains=query)
                | Q(email__icontains=query)
            )
        state = str(request.query_params.get('state') or '').strip()
        if state == 'active':
            queryset = queryset.filter(is_active=True, is_banned=False)
        elif state == 'banned':
            queryset = queryset.filter(is_banned=True)
        plan = str(request.query_params.get('plan') or '').strip()
        if plan in {User.PLAN_FREE, User.PLAN_PREMIUM}:
            queryset = queryset.filter(plan=plan)
        sort = str(request.query_params.get('sort') or 'time').strip()
        direction = 'asc' if request.query_params.get('direction') == 'asc' else 'desc'
        field = {'time': 'date_joined', 'name': 'first_name'}.get(sort, 'date_joined')
        queryset = queryset.order_by(field if direction == 'asc' else f'-{field}', '-id')

        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(AdminUserSerializer(page, many=True).data)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="جزئیات کاربر",
        description="دریافت اطلاعات کامل یک کاربر خاص بر اساس شناسه.",
        responses={200: AdminUserSerializer}
    )
    def get(self, request, pk):
        user = get_object_or_404(User.objects.select_related('artist_profile'), pk=pk)
        serializer = AdminUserSerializer(user)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کامل کاربر",
        description="ویرایش تمامی فیلدهای یک کاربر.",
        request=AdminUserSerializer,
        responses={200: AdminUserSerializer}
    )
    def put(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminUserSerializer(user, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="ویرایش جزئی کاربر",
        description="ویرایش برخی از فیلدهای یک کاربر.",
        request=AdminUserSerializer,
        responses={200: AdminUserSerializer}
    )
    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminUserSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف کاربر",
        description="حذف کامل یک کاربر از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserBanView(APIView):
    """Soft, reversible account blocking. User content is never deleted here."""
    permission_classes = [permissions.IsAdminUser]

    def post(self, request):
        user_id = request.data.get('user_id')
        banned = request.data.get('banned', True)
        if user_id in (None, ''):
            return Response({'detail': 'شناسه کاربر الزامی است.'}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(banned, str):
            banned = banned.strip().lower() in {'1', 'true', 'yes', 'on'}
        user = get_object_or_404(User, pk=user_id)
        if user.pk == request.user.pk:
            return Response({'detail': 'امکان مسدود کردن حساب مدیر فعلی وجود ندارد.'}, status=status.HTTP_400_BAD_REQUEST)
        if user.is_staff:
            return Response({'detail': 'حساب مدیر از این بخش قابل مسدودسازی نیست.'}, status=status.HTTP_400_BAD_REQUEST)

        user.is_banned = bool(banned)
        user.is_active = not bool(banned)
        user.save(update_fields=['is_banned', 'is_active'])
        return Response({
            'message': 'کاربر با موفقیت مسدود شد.' if banned else 'مسدودی کاربر با موفقیت برداشته شد.',
            'user': AdminUserSerializer(user).data,
        })


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminArtistListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        queryset = Artist.objects.select_related('user').all()
        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(
                Q(name__icontains=query) | Q(artistic_name__icontains=query)
                | Q(name_en__icontains=query) | Q(artistic_name_en__icontains=query)
                | Q(user__phone_number__icontains=query) | Q(email__icontains=query)
            )
        verified = request.query_params.get('verified')
        if verified in {'true', 'false'}:
            queryset = queryset.filter(verified=verified == 'true')
        queryset = queryset.order_by('-created_at', '-id')
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(AdminArtistSerializer(page, many=True).data)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminArtistDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات هنرمند",
        description="دریافت اطلاعات کامل یک هنرمند خاص.",
        responses={200: AdminArtistSerializer}
    )
    def get(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        serializer = AdminArtistSerializer(artist)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کامل هنرمند",
        description="ویرایش تمامی اطلاعات یک هنرمند.",
        request=AdminArtistSerializer,
        responses={200: AdminArtistSerializer}
    )
    def put(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        serializer = AdminArtistSerializer(artist, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="ویرایش جزئی هنرمند",
        description="ویرایش برخی از اطلاعات یک هنرمند.",
        request=AdminArtistSerializer,
        responses={200: AdminArtistSerializer}
    )
    def patch(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        serializer = AdminArtistSerializer(artist, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف هنرمند",
        description="حذف پروفایل هنرمند از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        # Artist deletion is intentionally guarded because several legacy relations
        # cascade from Artist (catalog and financial audit data). Blocking the linked
        # account is the safe reversible action for established artists.
        blockers = []
        if artist.songs.exists():
            blockers.append('آهنگ')
        if artist.albums.exists():
            blockers.append('آلبوم')
        if artist.deposit_requests.exists():
            blockers.append('سوابق تسویه')
        if artist.release_workspaces.exists():
            blockers.append('انتشار')
        if blockers:
            return Response(
                {
                    'detail': 'حذف دائمی این هنرمند به دلیل وجود اطلاعات وابسته مجاز نیست. برای توقف دسترسی، حساب مرتبط را مسدود کنید.',
                    'dependencies': blockers,
                },
                status=status.HTTP_409_CONFLICT,
            )
        artist.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPendingArtistListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست درخواست‌های هنرمند",
        description="دریافت لیست درخواست‌های عضویت هنرمندان که هنوز تایید یا رد نشده‌اند.",
        responses={200: AdminArtistAuthSerializer(many=True)}
    )
    def get(self, request):
        # records of artistAuth with not accepted or rejected status
        pending_auths = ArtistAuth.objects.exclude(
            status__in=[ArtistAuth.STATUS_ACCEPTED, ArtistAuth.STATUS_REJECTED]
        ).order_by('-created_at')
        
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(pending_auths, request)
        serializer = AdminArtistAuthSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPendingArtistDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="جزئیات درخواست هنرمند",
        description="دریافت جزئیات یک درخواست خاص برای بررسی.",
        responses={200: AdminArtistAuthSerializer}
    )
    def get(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کامل درخواست",
        description="ویرایش تمامی اطلاعات یک درخواست عضویت.",
        request=AdminArtistAuthSerializer,
        responses={200: AdminArtistAuthSerializer}
    )
    def put(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth, data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="تایید یا رد درخواست هنرمند",
        description="تغییر وضعیت درخواست هنرمند (تایید، رد یا در حال بررسی).",
        request=AdminArtistAuthSerializer,
        responses={200: AdminArtistAuthSerializer}
    )
    def patch(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف درخواست هنرمند",
        description="حذف یک درخواست عضویت از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        auth.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminHomeSummaryView(APIView):
    """Structured product dashboard: audience, artists, streams and money."""
    permission_classes = [permissions.IsAdminUser]

    @staticmethod
    def _decimal_total(queryset, field='amount'):
        value = queryset.aggregate(total=Sum(field))['total'] or Decimal('0')
        return float(value)

    def get(self, request):
        now = timezone.now()
        last_24 = now - timedelta(days=1)
        last_7 = now - timedelta(days=7)
        last_30 = now - timedelta(days=30)

        streams = PlayCount.objects.all()
        artist_earned_total = self._decimal_total(streams, 'pay')
        successful_payments = PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_SUCCESS)
        revenue_total = self._decimal_total(successful_payments)
        paid_payouts = DepositRequest.objects.filter(status=DepositRequest.STATUS_DONE)
        pending_payouts = DepositRequest.objects.filter(status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED])
        paid_payout_total = self._decimal_total(paid_payouts)
        pending_payout_total = self._decimal_total(pending_payouts)

        audience = User.objects.filter(roles__contains=User.ROLE_AUDIENCE)
        premium = audience.filter(plan=User.PLAN_PREMIUM, is_banned=False)
        artists = Artist.objects.all()
        top_artists = list(
            artists.annotate(
                total_streams=Count('songs__play_counts'),
                earned=Sum('songs__play_counts__pay'),
            ).order_by('-total_streams', '-created_at')[:6]
        )
        top_artist_payload = [{
            'id': artist.id,
            'name': artist.artistic_name or artist.name,
            'profile_image': generate_signed_r2_url(artist.profile_image) or artist.profile_image,
            'verified': artist.verified,
            'streams': int(getattr(artist, 'total_streams', 0) or 0),
            'earned': float(getattr(artist, 'earned', 0) or 0),
        } for artist in top_artists]

        return Response({
            'total': streams.count(),
            'last_30_days': streams.filter(created_at__gte=last_30).count(),
            'last_7_days': streams.filter(created_at__gte=last_7).count(),
            'last_24_hours': streams.filter(created_at__gte=last_24).count(),
            'total_pay': artist_earned_total,
            'pay_last_30_days': self._decimal_total(streams.filter(created_at__gte=last_30), 'pay'),
            'pay_last_7_days': self._decimal_total(streams.filter(created_at__gte=last_7), 'pay'),
            'pay_last_24_hours': self._decimal_total(streams.filter(created_at__gte=last_24), 'pay'),
            'audience_count': audience.count(),
            'artist_profiles_count': artists.count(),
            'users': {
                'total': audience.count(),
                'active': audience.filter(is_active=True, is_banned=False).count(),
                'banned': audience.filter(is_banned=True).count(),
                'premium': premium.count(),
                'free': audience.filter(plan=User.PLAN_FREE).count(),
                'new_30_days': audience.filter(date_joined__gte=last_30).count(),
            },
            'artists': {
                'total': artists.count(),
                'verified': artists.filter(verified=True).count(),
                'pending_verification': ArtistAuth.objects.exclude(
                    status__in=[ArtistAuth.STATUS_ACCEPTED, ArtistAuth.STATUS_REJECTED]
                ).count(),
                'successful': artists.filter(verified=True, songs__status=Song.STATUS_PUBLISHED).distinct().count(),
                'top': top_artist_payload,
            },
            'streams': {
                'total': streams.count(),
                'last_24_hours': streams.filter(created_at__gte=last_24).count(),
                'last_7_days': streams.filter(created_at__gte=last_7).count(),
                'last_30_days': streams.filter(created_at__gte=last_30).count(),
                'artist_earned_total': artist_earned_total,
            },
            'money': {
                'platform_revenue': revenue_total,
                'revenue_30_days': self._decimal_total(successful_payments.filter(created_at__gte=last_30)),
                'successful_payments_count': successful_payments.count(),
                'pending_payments_count': PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_PENDING).count(),
                'failed_payments_count': PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_FAILED).count(),
                'artist_earned_total': artist_earned_total,
                'artist_paid_total': paid_payout_total,
                'artist_pending_payout_total': pending_payout_total,
                'artist_pending_payout_count': pending_payouts.count(),
                'gross_after_paid_payouts': revenue_total - paid_payout_total,
            },
            'recent_transactions': AdminPaymentTransactionSerializer(
                PaymentTransaction.objects.select_related('user').all()[:5], many=True
            ).data,
            'recent_payouts': AdminDepositRequestSerializer(
                DepositRequest.objects.select_related('artist', 'artist__user').all()[:5], many=True
            ).data,
        })



@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserSearchView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        typ = str(request.query_params.get('type') or 'audience').strip()
        query = str(request.query_params.get('q') or '').strip()
        paginator = AdminPagination()
        if typ == 'audience':
            qs = User.objects.filter(roles__contains=User.ROLE_AUDIENCE)
            if query:
                qs = qs.filter(
                    Q(phone_number__icontains=query) | Q(unique_id__icontains=query)
                    | Q(first_name__icontains=query) | Q(last_name__icontains=query)
                    | Q(email__icontains=query)
                )
            qs = qs.order_by('-date_joined')
            serializer_cls = AdminUserSerializer
        elif typ == 'artist':
            qs = Artist.objects.select_related('user').all()
            if query:
                qs = qs.filter(
                    Q(name__icontains=query) | Q(artistic_name__icontains=query)
                    | Q(user__phone_number__icontains=query) | Q(email__icontains=query)
                )
            qs = qs.order_by('-created_at')
            serializer_cls = AdminArtistSerializer
        elif typ == 'pend_artist':
            qs = ArtistAuth.objects.exclude(status__in=[ArtistAuth.STATUS_ACCEPTED, ArtistAuth.STATUS_REJECTED])
            if query:
                qs = qs.filter(
                    Q(stage_name__icontains=query) | Q(first_name__icontains=query)
                    | Q(last_name__icontains=query) | Q(phone_number__icontains=query)
                    | Q(national_id__icontains=query)
                )
            qs = qs.order_by('-created_at')
            serializer_cls = AdminArtistAuthSerializer
        else:
            return Response({'detail': 'نوع جستجو معتبر نیست.'}, status=status.HTTP_400_BAD_REQUEST)
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(serializer_cls(page, many=True).data)



@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSongListView(APIView):
    """List songs for admin with status filtering."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست آهنگ‌ها",
        description="دریافت لیست تمامی آهنگ‌ها با قابلیت فیلتر بر اساس وضعیت (منتشر شده، در انتظار و غیره).",
        parameters=[
            OpenApiParameter("status", OpenApiTypes.STR, description="وضعیت آهنگ (مثلا published)", default="published")
        ],
        responses={200: AdminSongSerializer(many=True)}
    )
    def get(self, request):
        status_filter = str(request.query_params.get('status') or Song.STATUS_PUBLISHED).strip()
        sort = str(request.query_params.get('sort') or 'time').strip()
        direction = 'asc' if request.query_params.get('direction') == 'asc' else 'desc'

        songs = (
            Song.objects.exclude(status=Song.STATUS_DRAFT)
            .select_related('artist', 'album')
            .prefetch_related('featured_artists', 'genres', 'sub_genres', 'moods')
            .annotate(likes_count=Count('liked_by', distinct=True))
        )

        # The large play relation is deliberately absent from ordinary list SQL.
        # Only explicit play sorting needs a DB aggregate because Redis must not
        # become the authority for ordering.
        if sort == 'plays':
            songs = (
                songs.annotate(tracked_plays=Count('play_counts', distinct=True))
                .annotate(total_plays=F('plays') + F('tracked_plays'))
            )
        elif sort == 'meta':
            audio_score = Value(0, output_field=IntegerField())
            for field_name in AdminSongSerializer.AUDIO_CLASSIFICATION_FIELDS:
                audio_score = audio_score + Case(
                    When(**{f'{field_name}__isnull': False}, then=Value(1)),
                    default=Value(0), output_field=IntegerField(),
                )
            songs = (
                songs.annotate(
                    genre_count=Count('genres', distinct=True),
                    mood_count=Count('moods', distinct=True),
                    audio_meta_count=audio_score,
                )
                .annotate(
                    metadata_sort_score=(
                        Case(When(genre_count__gt=0, then=Value(7)), default=Value(0), output_field=IntegerField())
                        + Case(When(mood_count__gt=0, then=Value(7)), default=Value(0), output_field=IntegerField())
                        + F('audio_meta_count')
                    )
                )
            )

        if status_filter != 'all':
            songs = songs.filter(status=status_filter)
        query = str(request.query_params.get('q') or '').strip()
        if query:
            songs = songs.filter(
                Q(title__icontains=query) | Q(title_en__icontains=query)
                | Q(artist__name__icontains=query) | Q(artist__name_en__icontains=query)
                | Q(artist__artistic_name__icontains=query) | Q(artist__artistic_name_en__icontains=query)
            )

        field = {
            'time': 'created_at', 'plays': 'total_plays', 'likes': 'likes_count',
            'meta': 'metadata_sort_score', 'release': 'release_date',
        }.get(sort, 'created_at')
        songs = songs.order_by(field if direction == 'asc' else f'-{field}', '-id')
        paginator = AdminPagination()
        page = list(paginator.paginate_queryset(songs, request))
        if sort == 'plays':
            apply_annotated_song_play_counts(page)
        else:
            hydrate_song_play_counts(page)
        return paginator.get_paginated_response(AdminSongSerializer(page, many=True).data)


    @extend_schema(
        summary="آپلود آهنگ جدید توسط ادمین",
        description="آپلود فایل صوتی آهنگ به همراه متادیتا و تصویر کاور توسط ادمین برای هنرمند مشخص.",
        request=AdminSongSerializer,
        responses={201: AdminSongSerializer}
    )
    def post(self, request):
        serializer = AdminSongSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        
        try:
            # Get artist
            artist = data['artist']
            
            # Build filename: "Artist - Title (feat. X)" or "Artist - Title"
            title = data['title']
            featured_artists = data.get('featured_artists', [])
            featured_names = [a.artistic_name or a.name for a in featured_artists]
            
            artist_name = artist.artistic_name or artist.name
            if featured_names:
                filename_base = f"{artist_name} - {title} (feat. {', '.join(featured_names)})"
            else:
                filename_base = f"{artist_name} - {title}"
            
            safe_filename_base = make_safe_filename(filename_base)
            
            # Handle audio file upload
            audio_url = ""
            converted_audio_url = None
            duration = None
            original_format = None
            if 'audio_file_upload' in request.FILES:
                audio_file = request.FILES['audio_file_upload']
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
                if original_format != 'mp3' or bitrate is None or bitrate > 128:
                    try:
                        # Reset file pointer before conversion
                        if hasattr(audio_file, 'seek'):
                            audio_file.seek(0)
                        
                        converted_file = convert_to_128kbps(audio_file)
                        converted_filename = f"{safe_filename_base}_128.mp3"
                        converted_audio_url, _ = upload_file_to_r2(
                            converted_file,
                            folder='songs/128',
                            custom_filename=converted_filename
                        )
                    except Exception as e:
                        # Log error but don't fail the whole upload
                        print(f"Conversion failed: {e}")
            
            # Handle cover image upload
            cover_url = ""
            if 'cover_image_upload' in request.FILES:
                cover_file = request.FILES['cover_image_upload']
                cover_filename = f"{safe_filename_base}_cover.{cover_file.name.split('.')[-1]}"
                cover_url, _ = upload_file_to_r2(
                    cover_file,
                    folder='covers',
                    custom_filename=cover_filename
                )
            
            # Prepare song data
            song_data = dict(data)
            song_data['audio_file'] = audio_url
            song_data['converted_audio_url'] = converted_audio_url
            song_data['cover_image'] = cover_url
            song_data['original_format'] = original_format
            song_data['duration_seconds'] = duration
            song_data['uploader'] = request.user
            # Draft belongs to the artist workspace; admin-created catalog items enter review instead.
            if song_data.get('status', Song.STATUS_DRAFT) == Song.STATUS_DRAFT:
                song_data['status'] = Song.STATUS_PENDING
            
            # Remove file fields and many-to-many from data for create
            song_data.pop('audio_file_upload', None)
            song_data.pop('cover_image_upload', None)
            featured_artists = song_data.pop('featured_artists', [])
            genres = song_data.pop('genres', [])
            sub_genres = song_data.pop('sub_genres', [])
            moods = song_data.pop('moods', [])
            tags = song_data.pop('tags', [])
            
            song = Song.objects.create(**song_data)
            
            # Add many-to-many relationships
            song.featured_artists.set(featured_artists)
            song.genres.set(genres)
            song.sub_genres.set(sub_genres)
            song.moods.set(moods)
            song.tags.set(tags)
            
            return Response(
                AdminSongSerializer(song).data,
                status=status.HTTP_201_CREATED
            )
            
        except Artist.DoesNotExist:
            return Response(
                {'error': 'Artist not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        except Exception as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSongDetailView(APIView):
    """Retrieve, update or delete a song for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات آهنگ",
        description="دریافت اطلاعات کامل یک آهنگ خاص.",
        responses={200: AdminSongSerializer}
    )
    def get(self, request, pk):
        song = get_object_or_404(_admin_song_detail_queryset(), pk=pk)
        hydrate_song_play_counts([song])
        serializer = AdminSongSerializer(song)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش جزئی آهنگ",
        description="ویرایش برخی از فیلدهای آهنگ و آپلود فایل صوتی یا کاور جدید.",
        request=AdminSongSerializer,
        responses={200: AdminSongSerializer}
    )
    def patch(self, request, pk):
        song = get_object_or_404(Song.objects.exclude(status=Song.STATUS_DRAFT), pk=pk)
        return self._update_song(request, song, partial=True)

    @extend_schema(
        summary="ویرایش کامل آهنگ",
        description="ویرایش تمامی فیلدهای آهنگ.",
        request=AdminSongSerializer,
        responses={200: AdminSongSerializer}
    )
    def put(self, request, pk):
        song = get_object_or_404(Song.objects.exclude(status=Song.STATUS_DRAFT), pk=pk)
        return self._update_song(request, song, partial=False)

    @extend_schema(
        summary="حذف آهنگ",
        description="حذف کامل یک آهنگ از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        mode = str(request.query_params.get('mode') or 'hard').strip().lower()
        if mode not in {'soft', 'hard'}:
            return Response({'detail': 'mode must be soft or hard.'}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            song = get_object_or_404(
                Song.objects.select_for_update().exclude(status=Song.STATUS_DRAFT), pk=pk
            )
            if mode == 'soft':
                release_ids = list(song.release_track_links.values_list('release_id', flat=True))
                if song.status != Song.STATUS_DELETED:
                    song.status = Song.STATUS_DELETED
                    song.save(update_fields=['status', 'updated_at'])
                _take_down_empty_admin_releases(
                    release_ids, request.user, 'Song taken down by an administrator.'
                )
                return Response({'deletion': 'soft', 'id': song.pk}, status=status.HTTP_200_OK)
            _hard_delete_admin_song(song, request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _update_song(self, request, song, partial=False):
        data = request.data.copy()
        
        # Normalize repeated multipart list inputs, including explicit clears.
        list_fields = [
            'featured_artists', 'producers', 'producers_en', 'composers', 'composers_en',
            'lyricists', 'lyricists_en', 'genres', 'sub_genres', 'moods', 'tags',
        ]
        for field in list_fields:
            if field not in data or not hasattr(data, 'getlist'):
                continue
            values = data.getlist(field)
            if len(values) == 1 and ',' in values[0]:
                values = [value.strip() for value in values[0].split(',') if value.strip()]
            else:
                values = [value for value in values if str(value).strip()]
            if hasattr(data, 'setlist'):
                data.setlist(field, values)
            else:
                data[field] = values

        # Empty multipart values intentionally clear nullable metadata fields.
        for field in [
            'tempo', 'energy', 'danceability', 'valence', 'acousticness',
            'instrumentalness', 'speechiness', 'release_date',
        ]:
            if field in data and data.get(field) == '':
                data[field] = None

        # Handle audio file upload
        audio_file = request.FILES.get('audio_file_upload')
        if audio_file:
            title = data.get('title', song.title)
            artist = song.artist
            # If artist is being changed in the same request
            if 'artist' in data:
                try:
                    artist = Artist.objects.get(pk=data['artist'])
                except Artist.DoesNotExist:
                    pass
            
            artist_name = artist.artistic_name or artist.name
            
            duration, bitrate, format_ext = get_audio_info(audio_file)
            if not format_ext:
                _, ext = os.path.splitext(audio_file.name)
                format_ext = ext.lstrip('.').lower()
            
            # Build filename base
            featured_ids = data.get('featured_artists', [])
            if not featured_ids:
                # Fallback to current song featured artists if not in request
                featured_artists = song.featured_artists.all()
            else:
                featured_artists = Artist.objects.filter(id__in=featured_ids)
            
            featured_names = [a.artistic_name or a.name for a in featured_artists]
            
            if featured_names:
                filename_base = f"{artist_name} - {title} (feat. {', '.join(featured_names)})"
            else:
                filename_base = f"{artist_name} - {title}"
            
            safe_filename_base = filename_base
            audio_filename = f"{safe_filename_base}.{format_ext}"
            
            audio_url, _ = upload_file_to_r2(audio_file, folder='songs', custom_filename=audio_filename)
            data['audio_file'] = audio_url
            data['duration_seconds'] = duration
            data['original_format'] = format_ext
            
            # Handle 128kbps conversion
            if format_ext != 'mp3' or bitrate is None or bitrate > 128:
                try:
                    if hasattr(audio_file, 'seek'):
                        audio_file.seek(0)
                    converted_file = convert_to_128kbps(audio_file)
                    conv_filename = f"{safe_filename_base}_128.mp3"
                    converted_url, _ = upload_file_to_r2(converted_file, folder='songs/128', custom_filename=conv_filename)
                    data['converted_audio_url'] = converted_url
                except Exception as e:
                    print(f"Admin conversion failed: {e}")

        # Handle cover image upload
        cover_image = request.FILES.get('cover_image_upload')
        if cover_image:
            # Keep original name and format for cover image
            cover_url, _ = upload_file_to_r2(cover_image, folder='covers')
            data['cover_image'] = cover_url

        serializer = AdminSongSerializer(song, data=data, partial=partial)
        if serializer.is_valid():
            serializer.save()
            updated_song = get_object_or_404(_admin_song_detail_queryset(), pk=song.pk)
            hydrate_song_play_counts([updated_song])
            return Response(AdminSongSerializer(updated_song).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminReportListView(APIView):
    """List reports for admin with filtering."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست گزارش‌ها",
        description="دریافت لیست گزارش‌های تخلف ثبت شده توسط کاربران با قابلیت فیلتر بر اساس وضعیت بررسی و نوع هدف (آهنگ یا هنرمند).",
        parameters=[
            OpenApiParameter("has_reviewed", OpenApiTypes.BOOL, description="فیلتر بر اساس وضعیت بررسی شده"),
            OpenApiParameter("type", OpenApiTypes.STR, description="فیلتر بر اساس نوع: song یا artist")
        ],
        responses={200: AdminReportSerializer(many=True)}
    )
    def get(self, request):
        qs = Report.objects.all().order_by('-created_at')
        
        has_reviewed = request.query_params.get('has_reviewed')
        if has_reviewed is not None:
            qs = qs.filter(has_reviewed=has_reviewed.lower() == 'true')
            
        typ = request.query_params.get('type')
        if typ == 'song':
            qs = qs.filter(song__isnull=False)
        elif typ == 'artist':
            qs = qs.filter(artist__isnull=False)
        elif typ == 'user':
            qs = qs.filter(reported_user__isnull=False)
            
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(qs, request)
        serializer = AdminReportSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminReportDetailView(APIView):
    """Retrieve or update a report for admin."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="جزئیات گزارش",
        description="دریافت اطلاعات کامل یک گزارش خاص.",
        responses={200: AdminReportSerializer}
    )
    def get(self, request, pk):
        report = get_object_or_404(Report, pk=pk)
        serializer = AdminReportSerializer(report)
        return Response(serializer.data)

    @extend_schema(
        summary="بروزرسانی گزارش",
        description="تغییر وضعیت بررسی گزارش.",
        request=AdminReportSerializer,
        responses={200: AdminReportSerializer}
    )
    def put(self, request, pk):
        report = get_object_or_404(Report, pk=pk)
        data = request.data.copy()
        
        # If has_reviewed is being set to true, set reviewed_at
        if data.get('has_reviewed') is True or data.get('has_reviewed') == 'true':
            if not report.has_reviewed:
                data['reviewed_at'] = timezone.now()
        
        serializer = AdminReportSerializer(report, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف گزارش",
        description="حذف یک گزارش از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        report = get_object_or_404(Report, pk=pk)
        report.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPlayConfigurationView(APIView):
    """View for admin to manage global play and price settings."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="تنظیمات پخش و قیمت‌گذاری",
        description="دریافت تنظیمات کلی سیستم شامل قیمت هر پخش و غیره.",
        responses={200: AdminPlayConfigurationSerializer}
    )
    def get(self, request):
        config = PlayConfiguration.objects.last()
        if not config:
            config = PlayConfiguration.objects.create()
        serializer = AdminPlayConfigurationSerializer(config)
        return Response(serializer.data)

    @extend_schema(
        summary="بروزرسانی تنظیمات",
        description="تغییر تنظیمات کلی سیستم.",
        request=AdminPlayConfigurationSerializer,
        responses={200: AdminPlayConfigurationSerializer}
    )
    def post(self, request):
        config = PlayConfiguration.objects.last()
        if not config:
            config = PlayConfiguration.objects.create()
        
        serializer = AdminPlayConfigurationSerializer(config, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminBannerAdListView(APIView):
    """List and create banner ads for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست تبلیغات بنری",
        description="دریافت لیست تمامی بنرهای تبلیغاتی.",
        responses={200: AdminBannerAdSerializer(many=True)}
    )
    def get(self, request):
        ads = BannerAd.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(ads, request)
        serializer = AdminBannerAdSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد تبلیغ بنری جدید",
        description="آپلود تصویر و ایجاد یک بنر تبلیغاتی جدید.",
        request=AdminBannerAdSerializer,
        responses={201: AdminBannerAdSerializer}
    )
    def post(self, request):
        data = request.data.copy()
        image_file = request.FILES.get('image_upload')
        if image_file:
            safe_title = "".join([c for c in data.get('title', 'banner') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"banner_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            image_url, _ = upload_file_to_r2(image_file, folder='ads/banners', custom_filename=filename)
            data['image'] = image_url

        serializer = AdminBannerAdSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminBannerAdDetailView(APIView):
    """Retrieve, update or delete a banner ad for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات تبلیغ بنری",
        description="دریافت اطلاعات یک بنر تبلیغاتی خاص.",
        responses={200: AdminBannerAdSerializer}
    )
    def get(self, request, pk):
        ad = get_object_or_404(BannerAd, pk=pk)
        serializer = AdminBannerAdSerializer(ad)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش تبلیغ بنری",
        description="ویرایش اطلاعات یا تصویر یک بنر تبلیغاتی.",
        request=AdminBannerAdSerializer,
        responses={200: AdminBannerAdSerializer}
    )
    def patch(self, request, pk):
        ad = get_object_or_404(BannerAd, pk=pk)
        data = request.data.copy()
        image_file = request.FILES.get('image_upload')
        if image_file:
            safe_title = "".join([c for c in data.get('title', ad.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"banner_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            image_url, _ = upload_file_to_r2(image_file, folder='ads/banners', custom_filename=filename)
            data['image'] = image_url

        serializer = AdminBannerAdSerializer(ad, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف تبلیغ بنری",
        description="حذف یک بنر تبلیغاتی از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        ad = get_object_or_404(BannerAd, pk=pk)
        ad.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAudioAdListView(APIView):
    """List and create audio ads for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست تبلیغات صوتی",
        description="دریافت لیست تمامی تبلیغات صوتی.",
        responses={200: AdminAudioAdSerializer(many=True)}
    )
    def get(self, request):
        ads = AudioAd.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(ads, request)
        serializer = AdminAudioAdSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد تبلیغ صوتی جدید",
        description="آپلود فایل صوتی و کاور برای ایجاد یک تبلیغ صوتی جدید.",
        request=AdminAudioAdSerializer,
        responses={201: AdminAudioAdSerializer}
    )
    def post(self, request):
        data = request.data.dict() # Convert to dict to ensure manual overrides work
        # Accept either `file` (flat form-data) or legacy `audio_upload` field
        audio_file = request.FILES.get('file') or request.FILES.get('audio_upload')
        presigned_url = None
        if audio_file:
            safe_title = "".join([c for c in data.get('title', 'audio_ad') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            _, ext = os.path.splitext(audio_file.name)
            filename = f"audio_ad_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}{ext}"
            audio_url, _ = upload_file_to_r2(audio_file, folder='ads/audio', custom_filename=filename)
            data['audio_url'] = audio_url

            # generate a presigned (signed) URL for immediate use/testing
            try:
                presigned_url = generate_signed_r2_url(audio_url, expiration=3600)
            except Exception:
                presigned_url = None

            # Try to get duration if not provided
            if not data.get('duration'):
                duration, _, _ = get_audio_info(audio_file)
                if duration:
                    data['duration'] = duration

        image_file = request.FILES.get('image_cover_upload')
        if image_file:
            safe_title = "".join([c for c in data.get('title', 'audio_ad') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            _, ext = os.path.splitext(image_file.name)
            filename = f"audio_ad_cover_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}{ext}"
            image_url, _ = upload_file_to_r2(image_file, folder='ads/audio/covers', custom_filename=filename)
            data['image_cover'] = image_url

        serializer = AdminAudioAdSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            response_data = serializer.data
            # include uploaded URLs when available
            if data.get('audio_url'):
                response_data['original_url'] = data.get('audio_url')
                response_data['presigned_url'] = presigned_url
            return Response(response_data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAudioAdDetailView(APIView):
    """Retrieve, update or delete an audio ad for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات تبلیغ صوتی",
        description="دریافت اطلاعات یک تبلیغ صوتی خاص.",
        responses={200: AdminAudioAdSerializer}
    )
    def get(self, request, pk):
        ad = get_object_or_404(AudioAd, pk=pk)
        serializer = AdminAudioAdSerializer(ad)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش تبلیغ صوتی",
        description="ویرایش اطلاعات، فایل صوتی یا کاور یک تبلیغ صوتی.",
        request=AdminAudioAdSerializer,
        responses={200: AdminAudioAdSerializer}
    )
    def patch(self, request, pk):
        ad = get_object_or_404(AudioAd, pk=pk)
        data = request.data.dict() # Convert to dict
        
        audio_file = request.FILES.get('audio_upload')
        if audio_file:
            safe_title = "".join([c for c in data.get('title', ad.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            _, ext = os.path.splitext(audio_file.name)
            filename = f"audio_ad_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}{ext}"
            audio_url, _ = upload_file_to_r2(audio_file, folder='ads/audio', custom_filename=filename)
            data['audio_url'] = audio_url
            
            if not data.get('duration'):
                duration, _, _ = get_audio_info(audio_file)
                if duration:
                    data['duration'] = duration

        image_file = request.FILES.get('image_cover_upload')
        if image_file:
            safe_title = "".join([c for c in data.get('title', ad.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            _, ext = os.path.splitext(image_file.name)
            filename = f"audio_ad_cover_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}{ext}"
            image_url, _ = upload_file_to_r2(image_file, folder='ads/audio/covers', custom_filename=filename)
            data['image_cover'] = image_url

        serializer = AdminAudioAdSerializer(ad, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف تبلیغ صوتی",
        description="حذف یک تبلیغ صوتی از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        ad = get_object_or_404(AudioAd, pk=pk)
        ad.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAlbumListView(APIView):
    """List albums for admin."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست آلبوم‌ها",
        description="دریافت لیست تمامی آلبوم‌ها (به جز تک‌آهنگ‌ها) با قابلیت صفحه‌بندی.",
        responses={200: AdminAlbumSerializer(many=True)}
    )
    def get(self, request):
        visible_song_filter = ~Q(songs__status=Song.STATUS_DRAFT)
        qs = Album.objects.annotate(
            song_count=Count('songs', filter=visible_song_filter, distinct=True),
            active_song_count=Count(
                'songs', filter=visible_song_filter & ~Q(songs__status=Song.STATUS_DELETED), distinct=True
            ),
            visible_single_count=Count(
                'songs', filter=visible_song_filter & Q(songs__is_single=True), distinct=True
            ),
        ).filter(song_count__gt=0)
        qs = qs.exclude(song_count=1, visible_single_count=1).prefetch_related(
            Prefetch('songs', queryset=_admin_song_detail_queryset(), to_attr='_admin_visible_songs')
        )
        query = str(request.query_params.get('q') or '').strip()
        if query:
            qs = qs.filter(
                Q(title__icontains=query) | Q(title_en__icontains=query)
                | Q(artist__name__icontains=query) | Q(artist__name_en__icontains=query)
                | Q(artist__artistic_name__icontains=query) | Q(artist__artistic_name_en__icontains=query)
            )
        direction = 'asc' if request.query_params.get('direction') == 'asc' else 'desc'
        sort = str(request.query_params.get('sort') or 'time').strip()
        field = {'time': 'created_at', 'release': 'release_date', 'songs': 'song_count'}.get(sort, 'created_at')
        qs = qs.order_by(field if direction == 'asc' else f'-{field}', '-id')
        paginator = AdminPagination()
        page = paginator.paginate_queryset(qs, request)
        return paginator.get_paginated_response(AdminAlbumSerializer(page, many=True).data)



@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAlbumDetailView(APIView):
    """Retrieve, update or delete an album for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات آلبوم",
        description="دریافت اطلاعات کامل یک آلبوم خاص.",
        responses={200: AdminAlbumSerializer}
    )
    def get(self, request, pk):
        album = get_object_or_404(
            Album.objects.prefetch_related(
                Prefetch('songs', queryset=_admin_song_detail_queryset(), to_attr='_admin_visible_songs')
            ), pk=pk
        )
        serializer = AdminAlbumSerializer(album)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش جزئی آلبوم",
        description="ویرایش برخی از فیلدهای آلبوم و آپلود کاور جدید.",
        request=AdminAlbumSerializer,
        responses={200: AdminAlbumSerializer}
    )
    def patch(self, request, pk):
        album = get_object_or_404(Album, pk=pk)
        return self._update_album(request, album, partial=True)

    @extend_schema(
        summary="ویرایش کامل آلبوم",
        description="ویرایش تمامی فیلدهای آلبوم.",
        request=AdminAlbumSerializer,
        responses={200: AdminAlbumSerializer}
    )
    def put(self, request, pk):
        album = get_object_or_404(Album, pk=pk)
        return self._update_album(request, album, partial=False)

    @extend_schema(
        summary="حذف آلبوم",
        description="حذف کامل یک آلبوم از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        mode = str(request.query_params.get('mode') or 'hard').strip().lower()
        if mode not in {'soft', 'hard'}:
            return Response({'detail': 'mode must be soft or hard.'}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            album = get_object_or_404(Album.objects.select_for_update(), pk=pk)
            songs = Song.objects.select_for_update().filter(album=album)
            if mode == 'soft':
                release_ids = list(
                    ArtistReleaseTrack.objects.filter(song__album=album)
                    .values_list('release_id', flat=True).distinct()
                )
                affected = songs.exclude(status=Song.STATUS_DELETED).update(
                    status=Song.STATUS_DELETED, updated_at=timezone.now()
                )
                _take_down_empty_admin_releases(
                    release_ids, request.user, 'Album taken down by an administrator.'
                )
                _bump_catalog_after_commit()
                return Response({
                    'deletion': 'soft', 'id': album.pk, 'affected_songs': affected
                }, status=status.HTTP_200_OK)

            # Hard album deletion removes only the album record. Keep recordings as singles.
            songs.update(album=None, is_single=True, updated_at=timezone.now())
            album.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def _update_album(self, request, album, partial=False):
        data = request.data.copy()
        
        # Handle cover image upload
        cover_image = request.FILES.get('cover_image_upload')
        if cover_image:
            # Keep original name and format for cover image
            cover_url, _ = upload_file_to_r2(cover_image, folder='covers')
            data['cover_image'] = cover_url

        serializer = AdminAlbumSerializer(album, data=data, partial=partial)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAlbumSongActionView(APIView):
    """Actions on songs within an album: remove from album or delete song."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="عملیات روی آهنگ‌های آلبوم",
        description="حذف آهنگ از آلبوم یا حذف کامل آهنگ از سیستم.",
        request=inline_serializer(
            name='AdminAlbumSongActionRequest',
            fields={'action': serializers.ChoiceField(choices=['remove', 'delete'])}
        ),
        responses={
            200: inline_serializer(
                name='AdminAlbumSongActionResponse',
                fields={'message': serializers.CharField()}
            )
        }
    )
    def post(self, request, album_id, song_id):
        action = request.data.get('action') # 'remove' or 'delete'
        album = get_object_or_404(Album, pk=album_id)
        song = get_object_or_404(Song, pk=song_id, album=album)
        
        if action == 'remove':
            song.album = None
            song.save()
            return Response({"message": "Song removed from album"})
        elif action == 'delete':
            song.delete()
            return Response({"message": "Song deleted successfully"})
        else:
            return Response({"error": "Invalid action. Use 'remove' or 'delete'"}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminFinanceSummaryView(APIView):
    permission_classes = [permissions.IsAdminUser]

    @staticmethod
    def _total(qs, field='amount'):
        return float(qs.aggregate(total=Sum(field))['total'] or 0)

    def get(self, request):
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        last_7 = now - timedelta(days=7)
        last_30 = now - timedelta(days=30)

        def period(start_date, end_date=None):
            payments = PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_SUCCESS, created_at__gte=start_date)
            payouts_done = DepositRequest.objects.filter(status=DepositRequest.STATUS_DONE, submission_date__gte=start_date)
            payouts_open = DepositRequest.objects.filter(
                status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED], submission_date__gte=start_date
            )
            if end_date:
                payments = payments.filter(created_at__lte=end_date)
                payouts_done = payouts_done.filter(submission_date__lte=end_date)
                payouts_open = payouts_open.filter(submission_date__lte=end_date)
            return {
                'revenue': self._total(payments),
                'successful_payment_count': payments.count(),
                'paid_to_artists': self._total(payouts_done),
                'paid_to_artists_count': payouts_done.count(),
                'pending_artist_payouts': self._total(payouts_open),
                'pending_artist_payout_count': payouts_open.count(),
                'total_payments': self._total(payments),
                'total_deposits': self._total(payouts_done),
                'count_payments': payments.count(),
                'count_deposits': payouts_done.count(),
            }

        all_start = timezone.make_aware(timezone.datetime(2000, 1, 1))
        result = {
            'today': period(today_start),
            'last_7_days': period(last_7),
            'last_30_days': period(last_30),
            'all_time': period(all_start),
            'payment_status': {
                'pending': PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_PENDING).count(),
                'success': PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_SUCCESS).count(),
                'failed': PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_FAILED).count(),
            },
            'payout_status': {
                value: DepositRequest.objects.filter(status=value).count()
                for value in [DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_REJECTED, DepositRequest.STATUS_DONE]
            },
        }
        start_param = request.query_params.get('start')
        end_param = request.query_params.get('end')
        if start_param and end_param:
            try:
                start_dt = timezone.datetime.fromisoformat(start_param)
                end_dt = timezone.datetime.fromisoformat(end_param)
                if timezone.is_naive(start_dt):
                    start_dt = timezone.make_aware(start_dt)
                if timezone.is_naive(end_dt):
                    end_dt = timezone.make_aware(end_dt)
                if len(end_param) == 10:
                    end_dt = end_dt.replace(hour=23, minute=59, second=59)
                result['custom_period'] = period(start_dt, end_dt)
            except (TypeError, ValueError):
                result['custom_period_error'] = 'فرمت تاریخ معتبر نیست.'
        return Response(result)



@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminArtistEarningsListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        queryset = Artist.objects.select_related('user').annotate(
            stream_count=Count('songs__play_counts', distinct=True),
            earned_total=Sum('songs__play_counts__pay'),
        )
        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(
                Q(name__icontains=query) | Q(artistic_name__icontains=query)
                | Q(user__phone_number__icontains=query)
            )
        sort = str(request.query_params.get('sort') or 'earned').strip()
        direction = 'asc' if request.query_params.get('direction') == 'asc' else 'desc'
        field = 'stream_count' if sort == 'streams' else 'earned_total'
        queryset = queryset.order_by(field if direction == 'asc' else f'-{field}', '-id')

        paginator = AdminPagination()
        page = list(paginator.paginate_queryset(queryset, request))
        artist_ids = [artist.id for artist in page]
        payout_totals = {}
        if artist_ids:
            for row in (
                DepositRequest.objects.filter(artist_id__in=artist_ids)
                .values('artist_id', 'status')
                .annotate(total=Sum('amount'))
            ):
                payout_totals[(row['artist_id'], row['status'])] = row['total'] or Decimal('0')

        rows = []
        for artist in page:
            earned = artist.earned_total or Decimal('0')
            paid = payout_totals.get((artist.id, DepositRequest.STATUS_DONE), Decimal('0'))
            pending = (
                payout_totals.get((artist.id, DepositRequest.STATUS_PENDING), Decimal('0'))
                + payout_totals.get((artist.id, DepositRequest.STATUS_APPROVED), Decimal('0'))
            )
            rows.append({
                'artist_id': artist.id,
                'artist_name': artist.artistic_name or artist.name,
                'artist_phone': artist.user.phone_number if artist.user else None,
                'verified': artist.verified,
                'stream_count': int(artist.stream_count or 0),
                'earned_total': float(earned),
                'paid_total': float(paid),
                'pending_total': float(pending),
                'remaining_total': float(max(Decimal('0'), earned - paid - pending)),
            })
        response = paginator.get_paginated_response(rows)
        response.data['total_amount'] = float(PlayCount.objects.filter(songs__artist_id__in=queryset.values('id')).distinct().aggregate(total=Sum('pay'))['total'] or 0)
        return response


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPaymentTransactionListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        queryset = PaymentTransaction.objects.select_related('user').all()
        status_filter = str(request.query_params.get('status') or '').strip()
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(
                Q(transaction_id__icontains=query) | Q(user__phone_number__icontains=query)
                | Q(description__icontains=query)
            )
        sort = str(request.query_params.get('sort') or 'time').strip()
        direction = 'asc' if request.query_params.get('direction') == 'asc' else 'desc'
        field = 'amount' if sort == 'amount' else 'created_at'
        queryset = queryset.order_by(field if direction == 'asc' else f'-{field}', '-id')
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        response = paginator.get_paginated_response(AdminPaymentTransactionSerializer(page, many=True).data)
        response.data['total_amount'] = float(queryset.aggregate(total=Sum('amount'))['total'] or 0)
        response.data['total_count'] = queryset.count()
        return response



@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminDepositRequestListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        queryset = DepositRequest.objects.select_related('artist', 'artist__user').all()
        status_filter = str(request.query_params.get('status') or '').strip()
        if status_filter:
            statuses = [item for item in status_filter.split(',') if item]
            if status_filter == 'open':
                statuses = [DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED]
            queryset = queryset.filter(status__in=statuses)
        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(
                Q(transaction_id__icontains=query) | Q(artist__name__icontains=query)
                | Q(artist__artistic_name__icontains=query) | Q(artist__user__phone_number__icontains=query)
            )
        sort = str(request.query_params.get('sort') or 'time').strip()
        direction = 'asc' if request.query_params.get('direction') == 'asc' else 'desc'
        field = 'amount' if sort == 'amount' else 'submission_date'
        queryset = queryset.order_by(field if direction == 'asc' else f'-{field}', '-id')
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        response = paginator.get_paginated_response(AdminDepositRequestSerializer(page, many=True).data)
        response.data['total_amount'] = float(queryset.aggregate(total=Sum('amount'))['total'] or 0)
        response.data['total_count'] = queryset.count()
        return response



@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSearchSectionListView(APIView):
    """List and create search sections for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست بخش‌های جستجو",
        description="دریافت لیست تمامی بخش‌های (کتگوری‌های) صفحه جستجو.",
        responses={200: AdminSearchSectionSerializer(many=True)}
    )
    def get(self, request):
        sections = SearchSection.objects.all().order_by('-created_at')
        query = str(request.query_params.get('q') or '').strip()
        if query:
            sections = sections.filter(Q(title__icontains=query) | Q(title_en__icontains=query))
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(sections, request)
        serializer = AdminSearchSectionSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد بخش جستجوی جدید",
        description="ایجاد یک بخش جدید برای صفحه جستجو همراه با آیکون.",
        request=AdminSearchSectionSerializer,
        responses={201: AdminSearchSectionSerializer}
    )
    def post(self, request):
        data = request.data.copy()
        icon_file = request.FILES.get('icon_logo_upload')
        if icon_file:
            safe_title = "".join([c for c in data.get('title', 'section') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"section_icon_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            icon_url, _ = upload_file_to_r2(icon_file, folder='sections/icons', custom_filename=filename)
            data['icon_logo'] = icon_url

        serializer = AdminSearchSectionSerializer(data=data)
        if serializer.is_valid():
            serializer.save(created_by=request.user)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSearchSectionDetailView(APIView):
    """Retrieve, update or delete a search section for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات بخش جستجو",
        description="دریافت اطلاعات یک بخش خاص از صفحه جستجو.",
        responses={200: AdminSearchSectionSerializer}
    )
    def get(self, request, pk):
        section = get_object_or_404(SearchSection, pk=pk)
        serializer = AdminSearchSectionSerializer(section)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش بخش جستجو",
        description="ویرایش اطلاعات یا آیکون یک بخش از صفحه جستجو.",
        request=AdminSearchSectionSerializer,
        responses={200: AdminSearchSectionSerializer}
    )
    def patch(self, request, pk):
        section = get_object_or_404(SearchSection, pk=pk)
        data = request.data.copy()
        icon_file = request.FILES.get('icon_logo_upload')
        if icon_file:
            safe_title = "".join([c for c in data.get('title', section.title) if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"section_icon_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            icon_url, _ = upload_file_to_r2(icon_file, folder='sections/icons', custom_filename=filename)
            data['icon_logo'] = icon_url

        serializer = AdminSearchSectionSerializer(section, data=data, partial=True)
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
        section = get_object_or_404(SearchSection, pk=pk)
        section.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEventPlaylistListView(APIView):
    """List and create event playlist groups for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست گروه‌های پلی‌لیست رویداد",
        description="دریافت لیست تمامی گروه‌های پلی‌لیست مربوط به رویدادها.",
        responses={200: AdminEventPlaylistSerializer(many=True)}
    )
    def get(self, request):
        events = EventPlaylist.objects.all().order_by('-created_at')
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(events, request)
        serializer = AdminEventPlaylistSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد گروه پلی‌لیست رویداد جدید",
        description="ایجاد یک گروه جدید برای پلی‌لیست‌های رویداد همراه با کاور.",
        request=AdminEventPlaylistSerializer,
        responses={201: AdminEventPlaylistSerializer}
    )
    def post(self, request):
        data = request.data.copy()
        cover_file = request.FILES.get('cover_image_upload')
        if cover_file:
            safe_title = "".join([c for c in data.get('title', 'event') if c.isalnum() or c in (' ', '-', '_')]).rstrip()
            filename = f"event_cover_{safe_title}_{timezone.now().strftime('%Y%m%d%H%M%S')}"
            cover_url, _ = upload_file_to_r2(cover_file, folder='events/covers', custom_filename=filename)
            data['cover_image'] = cover_url

        serializer = AdminEventPlaylistSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEventPlaylistDetailView(APIView):
    """Retrieve, update or delete an event playlist group for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات گروه پلی‌لیست رویداد",
        description="دریافت اطلاعات یک گروه پلی‌لیست رویداد خاص.",
        responses={200: AdminEventPlaylistSerializer}
    )
    def get(self, request, pk):
        event = get_object_or_404(EventPlaylist, pk=pk)
        serializer = AdminEventPlaylistSerializer(event)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش گروه پلی‌لیست رویداد",
        description="ویرایش اطلاعات یا کاور یک گروه پلی‌لیست رویداد.",
        request=AdminEventPlaylistSerializer,
        responses={200: AdminEventPlaylistSerializer}
    )
    def patch(self, request, pk):
        event = get_object_or_404(EventPlaylist, pk=pk)
        data = request.data.copy()
        cover_file = request.FILES.get('cover_image_upload')
        if cover_file:
            # Keep original name and format for cover image
            cover_url, _ = upload_file_to_r2(cover_file, folder='events/covers')
            data['cover_image'] = cover_url

        serializer = AdminEventPlaylistSerializer(event, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف گروه پلی‌لیست رویداد",
        description="حذف یک گروه پلی‌لیست رویداد از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        event = get_object_or_404(EventPlaylist, pk=pk)
        event.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPlaylistBuilderView(APIView):
    """Fast song discovery + deterministic smart fill for official playlists."""
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        songs, source = _playlist_builder_queryset(request.query_params)
        paginator = AdminPagination()
        page = paginator.paginate_queryset(songs, request)
        response = paginator.get_paginated_response(_playlist_builder_song_data(page))
        response.data['source'] = source
        response.data['facets'] = _playlist_builder_facets()
        return response

    def post(self, request):
        mode = str(request.data.get('mode') or 'append').strip()
        if mode not in {'append', 'fill_to', 'replace'}:
            return Response({'detail': 'حالت تکمیل پلی‌لیست معتبر نیست.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            requested_count = max(1, min(500, int(request.data.get('count') or 1)))
            max_per_artist = max(0, min(100, int(request.data.get('max_per_artist') or 0)))
        except (TypeError, ValueError):
            return Response({'detail': 'تعداد واردشده معتبر نیست.'}, status=status.HTTP_400_BAD_REQUEST)

        existing_ids = _int_list(request.data.get('existing_ids'))
        if mode == 'append':
            needed = requested_count
            base_count = len(existing_ids)
            exclude_ids = existing_ids
        elif mode == 'fill_to':
            needed = max(0, requested_count - len(existing_ids))
            base_count = len(existing_ids)
            exclude_ids = existing_ids
        else:
            needed = requested_count
            base_count = 0
            exclude_ids = []

        params = request.data.copy()
        params['exclude'] = exclude_ids
        candidates, source = _playlist_builder_queryset(params)
        selected = []
        artist_counts = {}
        if max_per_artist and mode != 'replace' and existing_ids:
            artist_counts = {
                row['artist_id']: row['song_count']
                for row in Song.objects.filter(id__in=existing_ids)
                .values('artist_id')
                .annotate(song_count=Count('id'))
            }
        if needed:
            for song in candidates.iterator(chunk_size=200):
                if max_per_artist and artist_counts.get(song.artist_id, 0) >= max_per_artist:
                    continue
                selected.append(song)
                artist_counts[song.artist_id] = artist_counts.get(song.artist_id, 0) + 1
                if len(selected) >= needed:
                    break

        shortfall = max(0, needed - len(selected))
        return Response({
            'mode': mode,
            'source': source,
            'requested_count': requested_count,
            'add_count': len(selected),
            'final_count': base_count + len(selected),
            'shortfall': shortfall,
            'songs': _playlist_builder_song_data(selected),
        })


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPlaylistListView(APIView):
    """List and create playlists for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="لیست پلی‌لیست‌های ادمین",
        description="دریافت لیست تمامی پلی‌لیست‌های ایجاد شده توسط ادمین.",
        responses={200: AdminPlaylistSerializer(many=True)}
    )
    def get(self, request):
        playlists = Playlist.objects.all().order_by('-created_at')
        query = str(request.query_params.get('q') or '').strip()
        if query:
            playlists = playlists.filter(Q(title__icontains=query) | Q(title_en__icontains=query))
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(playlists, request)
        serializer = AdminPlaylistSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد پلی‌لیست جدید توسط ادمین",
        description="ایجاد یک پلی‌لیست جدید همراه با کاور توسط ادمین.",
        request=AdminPlaylistSerializer,
        responses={201: AdminPlaylistSerializer}
    )
    def post(self, request):
        data = request.data.copy()
        cover_file = request.FILES.get('cover_image_upload')
        if cover_file:
            # Keep original name and format for cover image
            cover_url, _ = upload_file_to_r2(cover_file, folder='playlists/covers')
            data['cover_image'] = cover_url

        serializer = AdminPlaylistSerializer(data=data)
        if serializer.is_valid():
            ordered_songs = serializer.validated_data.get('songs', [])
            with transaction.atomic():
                playlist = serializer.save(created_by=Playlist.CREATED_BY_ADMIN)
                if 'songs' in serializer.validated_data:
                    _set_playlist_song_order(playlist, ordered_songs)
            return Response(
                AdminPlaylistSerializer(playlist, context={'include_song_details': True}).data,
                status=status.HTTP_201_CREATED,
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPlaylistDetailView(APIView):
    """Retrieve, update or delete a playlist for admin."""
    permission_classes = [permissions.IsAdminUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات پلی‌لیست ادمین",
        description="دریافت اطلاعات کامل یک پلی‌لیست خاص.",
        responses={200: AdminPlaylistSerializer}
    )
    def get(self, request, pk):
        playlist = get_object_or_404(Playlist, pk=pk)
        serializer = AdminPlaylistSerializer(playlist, context={'include_song_details': True})
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش پلی‌لیست ادمین",
        description="ویرایش اطلاعات یا کاور یک پلی‌لیست.",
        request=AdminPlaylistSerializer,
        responses={200: AdminPlaylistSerializer}
    )
    def patch(self, request, pk):
        playlist = get_object_or_404(Playlist, pk=pk)
        data = request.data.copy()
        cover_file = request.FILES.get('cover_image_upload')
        if cover_file:
            # Keep original name and format for cover image
            cover_url, _ = upload_file_to_r2(cover_file, folder='playlists/covers')
            data['cover_image'] = cover_url

        serializer = AdminPlaylistSerializer(playlist, data=data, partial=True)
        if serializer.is_valid():
            has_song_update = 'songs' in serializer.validated_data
            ordered_songs = serializer.validated_data.get('songs', [])
            with transaction.atomic():
                playlist = serializer.save()
                if has_song_update:
                    _set_playlist_song_order(playlist, ordered_songs)
            return Response(AdminPlaylistSerializer(playlist, context={'include_song_details': True}).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف پلی‌لیست ادمین",
        description="حذف یک پلی‌لیست از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        playlist = get_object_or_404(Playlist, pk=pk)
        playlist.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEmployeeListView(APIView):
    """List and create employees (managers/supervisors)."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="لیست کارمندان",
        description="دریافت لیست تمامی کارمندان (مدیران و ناظران) سیستم.",
        responses={200: AdminEmployeeSerializer(many=True)}
    )
    def get(self, request):
        # Filter users with manager or supervisor roles who are not staff
        queryset = User.objects.filter(
            Q(roles__contains=User.ROLE_MANAGER) | Q(roles__contains=User.ROLE_SUPERVISOR),
            is_staff=False
        ).order_by('-date_joined')
        
        paginator = AdminPagination()
        result_page = paginator.paginate_queryset(queryset, request)
        serializer = AdminEmployeeSerializer(result_page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @extend_schema(
        summary="ایجاد کارمند جدید",
        description="ایجاد یک کاربر جدید با نقش مدیر یا ناظر.",
        request=AdminEmployeeSerializer,
        responses={201: AdminEmployeeSerializer}
    )
    def post(self, request):
        serializer = AdminEmployeeSerializer(data=request.data)
        if serializer.is_valid():
            # Ensure is_staff is False and roles are restricted to manager/supervisor
            roles = serializer.validated_data.get('roles', [])
            if not any(role in [User.ROLE_MANAGER, User.ROLE_SUPERVISOR] for role in roles):
                return Response({"error": "User must have manager or supervisor role."}, status=status.HTTP_400_BAD_REQUEST)
            
            serializer.save(is_staff=False)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEmployeeDetailView(APIView):
    """Retrieve, update or delete an employee."""
    permission_classes = [permissions.IsAdminUser]

    @extend_schema(
        summary="جزئیات کارمند",
        description="دریافت اطلاعات کامل یک کارمند خاص.",
        responses={200: AdminEmployeeSerializer}
    )
    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminEmployeeSerializer(user)
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کارمند",
        description="ویرایش اطلاعات یا نقش‌های یک کارمند.",
        request=AdminEmployeeSerializer,
        responses={200: AdminEmployeeSerializer}
    )
    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        serializer = AdminEmployeeSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @extend_schema(
        summary="حذف کارمند",
        description="حذف یک کارمند از سیستم.",
        responses={204: None}
    )
    def delete(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get(self, request, pk):
        user = get_object_or_404(User, pk=pk, is_staff=False)
        if not any(role in [User.ROLE_MANAGER, User.ROLE_SUPERVISOR] for role in (user.roles or [])):
            return Response({"error": "Not an employee."}, status=status.HTTP_404_NOT_FOUND)
        serializer = AdminEmployeeSerializer(user)
        return Response(serializer.data)

    def patch(self, request, pk):
        user = get_object_or_404(User, pk=pk, is_staff=False)
        serializer = AdminEmployeeSerializer(user, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        user = get_object_or_404(User, pk=pk, is_staff=False)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)



@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminDepositRequestDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def patch(self, request, pk):
        deposit = get_object_or_404(DepositRequest.objects.select_related('artist'), pk=pk)
        new_status = str(request.data.get('status') or deposit.status).strip()
        valid_statuses = {value for value, _ in DepositRequest.STATUS_CHOICES}
        if new_status not in valid_statuses:
            return Response({'detail': 'وضعیت تسویه معتبر نیست.'}, status=status.HTTP_400_BAD_REQUEST)
        transaction_id = request.data.get('transaction_id', deposit.transaction_id)
        allowed_transitions = {
            DepositRequest.STATUS_PENDING: {DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_REJECTED},
            DepositRequest.STATUS_APPROVED: {DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_DONE, DepositRequest.STATUS_REJECTED},
            DepositRequest.STATUS_REJECTED: {DepositRequest.STATUS_REJECTED, DepositRequest.STATUS_PENDING},
            DepositRequest.STATUS_DONE: {DepositRequest.STATUS_DONE},
        }
        if new_status not in allowed_transitions.get(deposit.status, {deposit.status}):
            return Response(
                {'detail': 'تغییر وضعیت تسویه از وضعیت فعلی به وضعیت انتخاب‌شده مجاز نیست.'},
                status=status.HTTP_409_CONFLICT,
            )
        if new_status == DepositRequest.STATUS_DONE and not str(transaction_id or '').strip():
            return Response({'detail': 'برای ثبت تسویه انجام‌شده، شماره تراکنش الزامی است.'}, status=status.HTTP_400_BAD_REQUEST)
        changed = []
        if new_status != deposit.status:
            deposit.status = new_status
            deposit.status_change_date = timezone.now()
            changed.extend(['status', 'status_change_date'])
        if transaction_id != deposit.transaction_id:
            deposit.transaction_id = str(transaction_id or '').strip() or None
            changed.append('transaction_id')
        if changed:
            deposit.save(update_fields=changed)
        return Response(AdminDepositRequestSerializer(deposit).data)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSystemStatusView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        import time
        r2_ok = False
        start = time.perf_counter()
        detail = 'ارتباط احراز‌شده با فضای ذخیره‌سازی برقرار نشد.'
        try:
            check_r2_storage()
            r2_ok = True
            detail = 'فضای ذخیره‌سازی خصوصی R2 در دسترس است.'
        except Exception:
            pass
        latency_ms = round((time.perf_counter() - start) * 1000)
        return Response({
            'api': {'ok': True, 'label': 'سرور API', 'detail': 'API در دسترس است.'},
            'r2': {'ok': r2_ok, 'label': 'فضای R2', 'detail': detail, 'latency_ms': latency_ms},
            'checked_at': timezone.now().isoformat(),
        })


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSupportTicketListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        queryset = SupportTicket.objects.select_related('user', 'responded_by', 'user__artist_profile').all()
        status_filter = str(request.query_params.get('status') or '').strip()
        if status_filter:
            queryset = queryset.filter(status=status_filter)
        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(
                Q(subject__icontains=query) | Q(message__icontains=query)
                | Q(user__phone_number__icontains=query) | Q(user__artist_profile__name__icontains=query)
                | Q(user__artist_profile__artistic_name__icontains=query)
            )
        queryset = queryset.order_by('-created_at', '-id')
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(AdminSupportTicketSerializer(page, many=True).data)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSupportTicketDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request, pk):
        ticket = get_object_or_404(SupportTicket.objects.select_related('user', 'responded_by'), pk=pk)
        return Response(AdminSupportTicketSerializer(ticket).data)

    def patch(self, request, pk):
        ticket = get_object_or_404(SupportTicket, pk=pk)
        serializer = AdminSupportTicketSerializer(ticket, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        response_changed = 'admin_response' in serializer.validated_data
        ticket = serializer.save()
        if response_changed and ticket.admin_response.strip():
            ticket.responded_by = request.user
            ticket.responded_at = timezone.now()
            if 'status' not in serializer.validated_data and ticket.status != SupportTicket.STATUS_CLOSED:
                ticket.status = SupportTicket.STATUS_ANSWERED
            ticket.save(update_fields=['responded_by', 'responded_at', 'status', 'updated_at'])
        return Response(AdminSupportTicketSerializer(ticket).data)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSongPromotionListView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        now = timezone.now()
        queryset = SongPromotion.objects.select_related('song', 'song__artist', 'created_by').all()
        state = str(request.query_params.get('state') or '').strip()
        if state == 'running':
            queryset = queryset.filter(is_active=True, starts_at__lte=now, ends_at__gt=now)
        elif state == 'upcoming':
            queryset = queryset.filter(is_active=True, starts_at__gt=now)
        elif state == 'ended':
            queryset = queryset.filter(ends_at__lte=now)
        elif state == 'disabled':
            queryset = queryset.filter(is_active=False)
        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(
                Q(song__title__icontains=query) | Q(song__title_en__icontains=query)
                | Q(song__artist__name__icontains=query) | Q(song__artist__name_en__icontains=query)
                | Q(song__artist__artistic_name__icontains=query) | Q(song__artist__artistic_name_en__icontains=query)
            )
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(AdminSongPromotionSerializer(page, many=True).data)

    def post(self, request):
        serializer = AdminSongPromotionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        promotion = serializer.save(created_by=request.user)
        return Response(AdminSongPromotionSerializer(promotion).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSongPromotionDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def patch(self, request, pk):
        promotion = get_object_or_404(SongPromotion, pk=pk)
        serializer = AdminSongPromotionSerializer(promotion, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(AdminSongPromotionSerializer(serializer.save()).data)

    def delete(self, request, pk):
        promotion = get_object_or_404(SongPromotion, pk=pk)
        promotion.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
