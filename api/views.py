from rest_framework import generics, permissions, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.decorators import action
import logging
import re
import uuid
from decimal import Decimal, ROUND_DOWN
from .models import (
    User, Artist, Album, ArtistRelease, ArtistReleaseStatusHistory, ArtistReleaseTrack, Playlist,NotificationSetting, Genre, Mood, Tag, SubGenre, Song,
    StreamAccess, PlayCount, UserPlaylist, RecommendedPlaylist, EventPlaylist, SearchSection,
    ArtistMonthlyListener, UserHistory, Follow, SongLike, AlbumLike, PlaylistLike, Rules, PlayConfiguration,
    ActivePlayback, DepositRequest, Report, Notification, AudioAd, ArtistSocialAccount, SocialPlatform, DownloadHistory,
    InitialCheck, UserImageProfile
)
from .models import BannerAd, BannerAdServeCounter
from .localization import generated_term_en, get_request_language
from .realtime_notifications import (
    publish_all_notifications_read,
    publish_notification_read,
)
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
from django.db import connection, transaction
from django.db.models import (
    Sum, Count, F, IntegerField, BigIntegerField, Value, Prefetch, DecimalField, CharField, ExpressionWrapper,
    TextField, OuterRef, Subquery, Max, Case, When,
)
from django.db.models.functions import Coalesce, TruncDate, TruncHour, TruncWeek, TruncMonth, Replace, Cast, Concat
from django.utils import timezone
from django.conf import settings
from django.http import StreamingHttpResponse
from django.core.cache import cache
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from .utils import (
    MediaPipelineError, absolute_api_url, artist_filename_name, cleanup_r2_urls,
    convert_to_128kbps, generate_signed_r2_url, get_audio_info,
    upload_audio_variants, upload_file_to_r2,
)
from .auth_views import normalize_phone, create_and_send_otp, OtpCode
import boto3
import requests
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
from datetime import date, datetime, timedelta
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
    hydrate_song_metrics, stable_cache_key, user_affinity_version,
)
from collections import Counter
import json
from .subscriptions import activate_one_month_premium_locked
from .recommendation_runtime import (
    fresh_order_ids, fresh_order_objects, fresh_select_ids,
    mark_generated_playlist_usage, remember_exposure,
)
from .release_service import mark_release_for_review, merged_release_metadata, merged_shared

logger = logging.getLogger(__name__)

FINANCE_QUANTUM = Decimal('0.00000001')


def _finance_decimal(value):
    return Decimal(str(value or 0)).quantize(FINANCE_QUANTUM)


def _finance_string(value):
    return format(_finance_decimal(value), 'f')


def _finance_output_field():
    return DecimalField(max_digits=20, decimal_places=8)


def _finance_zero():
    return Value(Decimal('0.00000000'), output_field=_finance_output_field())


def _finance_day_start(value):
    return timezone.make_aware(
        datetime.combine(value, datetime.min.time()),
        timezone.get_current_timezone(),
    )


def _finance_month_start(value):
    return value.replace(day=1)


def _finance_shift_month(value, offset):
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _finance_bucket_key(value, group):
    if isinstance(value, datetime):
        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if group == 'monthly':
        return _finance_month_start(value)
    if group == 'weekly':
        return value - timedelta(days=value.weekday())
    return value


def _finance_bucket_range(group, start, end):
    current = start
    while current <= end:
        yield current
        current = _finance_shift_month(current, 1) if group == 'monthly' else current + timedelta(days=7 if group == 'weekly' else 1)


def _finance_song_totals(artist):
    rows = Song.objects.filter(artist=artist).annotate(
        finance_income=Coalesce(Sum('play_counts__pay'), _finance_zero()),
    ).values_list('id', 'finance_income')
    return {int(song_id): _finance_decimal(income) for song_id, income in rows}


def _finance_saved_song_allocations(summary):
    if not isinstance(summary, dict):
        return {}
    raw = summary.get('song_allocations')
    if isinstance(raw, dict):
        items = raw.items()
    elif isinstance(raw, list):
        items = ((item.get('song_id'), item.get('amount')) for item in raw if isinstance(item, dict))
    else:
        return {}

    allocations = {}
    for song_id, amount in items:
        try:
            song_id = int(song_id)
            value = max(Decimal('0'), _finance_decimal(amount))
        except (TypeError, ValueError, ArithmeticError):
            continue
        if value:
            allocations[song_id] = allocations.get(song_id, Decimal('0')) + value
    return allocations


def _finance_allocate_across_songs(song_totals, already_allocated, amount):
    target = max(Decimal('0'), _finance_decimal(amount))
    available = {
        song_id: max(Decimal('0'), _finance_decimal(total) - _finance_decimal(already_allocated.get(song_id, 0)))
        for song_id, total in song_totals.items()
    }
    available = {song_id: value for song_id, value in available.items() if value > 0}
    available_total = sum(available.values(), Decimal('0'))
    target = min(target, available_total)
    if target <= 0 or available_total <= 0:
        return {}

    ordered = sorted(available.items(), key=lambda item: (-item[1], item[0]))
    allocations = {}
    allocated_total = Decimal('0')
    for song_id, balance in ordered:
        share = (target * balance / available_total).quantize(FINANCE_QUANTUM, rounding=ROUND_DOWN)
        share = min(balance, share)
        if share > 0:
            allocations[song_id] = share
            allocated_total += share

    remainder = target - allocated_total
    if remainder > 0:
        for song_id, balance in ordered:
            capacity = balance - allocations.get(song_id, Decimal('0'))
            if capacity <= 0:
                continue
            addition = min(capacity, remainder)
            allocations[song_id] = allocations.get(song_id, Decimal('0')) + addition
            remainder -= addition
            if remainder <= 0:
                break

    return {song_id: _finance_decimal(value) for song_id, value in allocations.items() if value > 0}


def _finance_artist_song_allocations(artist, song_totals=None, requests=None):
    song_totals = song_totals if song_totals is not None else _finance_song_totals(artist)
    requests = requests if requests is not None else DepositRequest.objects.filter(
        artist=artist,
        status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_DONE],
    ).order_by('submission_date', 'pk')

    reserved = {song_id: Decimal('0') for song_id in song_totals}
    deposited = {song_id: Decimal('0') for song_id in song_totals}
    pending = {song_id: Decimal('0') for song_id in song_totals}

    for payout in requests:
        saved = _finance_saved_song_allocations(payout.summary)
        valid_saved = {
            song_id: min(value, max(Decimal('0'), song_totals.get(song_id, Decimal('0')) - reserved.get(song_id, Decimal('0'))))
            for song_id, value in saved.items()
            if song_id in song_totals and value > 0
        }
        requested_amount = max(Decimal('0'), _finance_decimal(payout.amount))
        saved_total = sum(valid_saved.values(), Decimal('0'))
        if not valid_saved or abs(saved_total - requested_amount) > FINANCE_QUANTUM:
            allocation = _finance_allocate_across_songs(song_totals, reserved, requested_amount)
        else:
            allocation = valid_saved

        for song_id, value in allocation.items():
            reserved[song_id] = reserved.get(song_id, Decimal('0')) + value
            if payout.status == DepositRequest.STATUS_DONE:
                deposited[song_id] = deposited.get(song_id, Decimal('0')) + value
            else:
                pending[song_id] = pending.get(song_id, Decimal('0')) + value

    return reserved, deposited, pending


def _artist_album_payload(album, serialized, songs=None):
    """Add artist-only operational stats without changing public album serializers."""
    tracks = list(songs if songs is not None else album.songs.all())
    active = [song for song in tracks if song.status != Song.STATUS_DELETED]
    total_income = sum((_finance_decimal(getattr(song, 'artist_income', 0)) for song in tracks), Decimal('0'))
    total_streams = sum(
        int(getattr(song, 'plays', 0) or 0) + int(getattr(song, 'artist_tracked_plays', 0) or 0)
        for song in tracks
    )
    total_duration = sum(int(getattr(song, 'duration_seconds', 0) or 0) for song in active)
    return {
        **dict(serialized),
        'active_songs_count': len(active),
        'deleted_songs_count': len(tracks) - len(active),
        'published_songs_count': sum(song.status == Song.STATUS_PUBLISHED for song in active),
        'total_streams': total_streams,
        'total_income': _finance_string(total_income),
        'total_duration_seconds': total_duration,
    }


_ARTIST_UPLOAD_ID_RE = re.compile(r'^[A-Za-z0-9_-]{16,96}$')


def _artist_upload_id(value):
    token = str(value or '').strip()
    return token if _ARTIST_UPLOAD_ID_RE.fullmatch(token) else ''


def _artist_upload_cache_key(user_id, upload_id):
    return f'artist-song-upload:{user_id}:{upload_id}'


def _set_artist_upload_state(user_id, upload_id, state, **extra):
    if not upload_id:
        return
    payload = {
        'state': state,
        'updated_at': timezone.now().isoformat(),
        **extra,
    }
    cache.set(
        _artist_upload_cache_key(user_id, upload_id),
        payload,
        timeout=int(getattr(settings, 'ARTIST_UPLOAD_RECOVERY_TTL', 3600)),
    )


def _get_artist_upload_state(user_id, upload_id):
    if not upload_id:
        return None
    return cache.get(_artist_upload_cache_key(user_id, upload_id))



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


def _cleanup_unreferenced_song_media(urls):
    for url in dict.fromkeys(value for value in urls if value):
        if Song.objects.filter(
            Q(audio_file=url) | Q(converted_audio_url=url) | Q(preview_audio_url=url) | Q(cover_image=url)
        ).exists():
            continue
        if Album.objects.filter(cover_image=url).exists():
            continue
        if ArtistRelease.objects.filter(release_metadata__cover_url=url).exists():
            continue
        cleanup_r2_urls([url])


def _artist_panel_release_links_prefetch():
    return Prefetch(
        'release_track_links',
        queryset=ArtistReleaseTrack.objects.select_related('release').order_by('-release__updated_at', '-id'),
        to_attr='_artist_panel_release_links',
    )


def _apply_release_cover_fallback(song, payload):
    """Add artist-workflow context and use linked release artwork as fallback."""
    links = getattr(song, '_artist_panel_release_links', None)
    if links is None:
        links = list(song.release_track_links.select_related('release').order_by('-release__updated_at', '-id'))
    else:
        links = list(links)
    payload['linked_release_ids'] = [str(link.release_id) for link in links]
    payload['linked_release_statuses'] = list(dict.fromkeys(link.release.status for link in links))
    payload['requires_reapproval'] = (
        song.status in {Song.STATUS_APPROVED, Song.STATUS_PUBLISHED}
        or any(link.release.status not in {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW} for link in links)
    )
    inherited_cover = False
    for link in links:
        source = str((link.extras or {}).get('_cover_source') or '')
        if payload.get('cover_image') and source != 'release':
            break
        cover = str((link.release.release_metadata or {}).get('cover_url') or '').strip()
        if cover:
            payload['cover_image'] = cover
            payload['release_id'] = str(link.release_id)
            payload['release_type'] = link.release.release_type
            inherited_cover = True
            break
    payload['own_cover_image'] = bool(payload.get('cover_image')) and not inherited_cover
    return payload


def _sync_release_from_artist_song(release, song, *, cover_changed=False):
    """Keep single-release metadata synchronized without leaking one album track onto its siblings."""
    release.validation_snapshot = {}
    update_fields = ['validation_snapshot', 'updated_at']

    if release.release_type != ArtistRelease.TYPE_SINGLE and cover_changed:
        link = ArtistReleaseTrack.objects.filter(release=release, song=song).first()
        if link:
            extras = dict(link.extras or {})
            extras['_cover_source'] = 'track'
            link.extras = extras
            link.save(update_fields=['extras', 'updated_at'])

    if release.release_type == ArtistRelease.TYPE_SINGLE:
        shared = dict(release.shared_metadata or {})
        shared.update({
            'language': song.language or 'fa',
            'label': song.label or '',
            'label_en': song.label_en or '',
            'genre_ids': list(song.genres.values_list('id', flat=True)),
            'sub_genre_ids': list(song.sub_genres.values_list('id', flat=True)),
            'mood_ids': list(song.moods.values_list('id', flat=True)),
            'tag_ids': list(song.tags.values_list('id', flat=True)),
            'producers': list(song.producers or []),
            'producers_en': list(song.producers_en or []),
            'composers': list(song.composers or []),
            'composers_en': list(song.composers_en or []),
            'lyricists': list(song.lyricists or []),
            'lyricists_en': list(song.lyricists_en or []),
        })
        release.shared_metadata = merged_shared(shared)
        metadata = merged_release_metadata(release.release_metadata, release.artist_id)
        if song.release_date:
            metadata['release_date'] = song.release_date.isoformat()
        if cover_changed and song.cover_image:
            metadata['cover_url'] = song.cover_image
        release.release_metadata = metadata
        release.title = song.title
        release.title_en = song.title_en or ''
        update_fields.extend(['title', 'title_en', 'shared_metadata', 'release_metadata'])

    release.save(update_fields=update_fields)


def _serialize_artist_songs(songs, request):
    songs = list(songs)
    data = list(SongSerializer(songs, many=True, context={'request': request}).data)
    return [_apply_release_cover_fallback(song, item) for song, item in zip(songs, data)]


def _renumber_release_tracks(release_ids):
    for release_id in set(release_ids):
        links = list(
            ArtistReleaseTrack.objects.select_for_update()
            .filter(release_id=release_id)
            .order_by('position', 'id')
        )
        for index, link in enumerate(links, start=1):
            if link.position != index:
                ArtistReleaseTrack.objects.filter(pk=link.pk).update(position=1000 + index)
        for index, link in enumerate(links, start=1):
            ArtistReleaseTrack.objects.filter(pk=link.pk).update(position=index)


def _album_is_deleted(album):
    return album.songs.exists() and not album.songs.exclude(status=Song.STATUS_DELETED).exists()


def _mark_releases_without_active_tracks(release_ids, actor=None):
    """Disable historical releases and remove empty album drafts; return orphan candidates."""
    media_urls = []
    for release in ArtistRelease.objects.select_for_update().filter(pk__in=set(release_ids)).order_by('pk'):
        if release.release_tracks.exclude(song__status=Song.STATUS_DELETED).exists():
            continue
        if release.status == ArtistRelease.STATUS_DRAFT:
            if release.release_type == ArtistRelease.TYPE_ALBUM:
                cover = str((release.release_metadata or {}).get('cover_url') or '').strip()
                if cover:
                    media_urls.append(cover)
                release.delete()
            continue
        if release.status != ArtistRelease.STATUS_TAKEN_DOWN:
            previous = release.status
            release.status = ArtistRelease.STATUS_TAKEN_DOWN
            release.taken_down_at = timezone.now()
            release.validation_snapshot = {}
            release.lock_version += 1
            release.save(update_fields=['status', 'taken_down_at', 'validation_snapshot', 'lock_version', 'updated_at'])
            ArtistReleaseStatusHistory.objects.create(
                release=release,
                from_status=previous,
                to_status=ArtistRelease.STATUS_TAKEN_DOWN,
                note='All active recordings were removed or deleted by the artist.',
                actor=actor,
            )
    return media_urls


def _delete_artist_song_locked(song, actor=None):
    """Apply artist deletion without breaking release/accounting foreign keys."""
    links = list(
        ArtistReleaseTrack.objects.select_for_update()
        .select_related('release')
        .filter(song=song)
    )
    draft_links = [link for link in links if link.release.status == ArtistRelease.STATUS_DRAFT]
    draft_release_ids = {link.release_id for link in draft_links}
    if draft_links:
        ArtistReleaseTrack.objects.filter(pk__in=[link.pk for link in draft_links]).delete()
        _renumber_release_tracks(draft_release_ids)

    release_ids = {link.release_id for link in links}
    if release_ids:
        ArtistRelease.objects.filter(pk__in=release_ids).update(
            validation_snapshot={},
            lock_version=F('lock_version') + 1,
            updated_at=timezone.now(),
        )

    non_draft_linked = any(link.release.status != ArtistRelease.STATUS_DRAFT for link in links)
    has_accounting = bool(song.plays) or song.play_counts.exists()
    must_preserve = song.status in {Song.STATUS_PUBLISHED, Song.STATUS_DELETED} or non_draft_linked or has_accounting
    if must_preserve:
        if song.status != Song.STATUS_DELETED:
            song.status = Song.STATUS_DELETED
            song.save(update_fields=['status', 'updated_at'])
        media_urls = _mark_releases_without_active_tracks(release_ids, actor=actor)
        return 'soft', media_urls

    media_urls = list(filter(None, [
        song.audio_file,
        song.converted_audio_url,
        song.preview_audio_url,
        song.cover_image,
    ]))
    media_urls.extend(_mark_releases_without_active_tracks(release_ids, actor=actor))
    song.delete()
    return 'hard', media_urls


def _touch_user_history(user, content_type, **target):
    """Atomically touch one history row and collapse legacy duplicates.

    PostgreSQL advisory locks serialize concurrent requests for the same user/item
    even though older databases do not have a matching unique constraint. This
    avoids ``MultipleObjectsReturned`` without requiring a migration.
    """
    target_ids = [value.pk if hasattr(value, 'pk') else int(value) for value in target.values() if value is not None]
    target_id = target_ids[0] if target_ids else 0
    type_codes = {
        UserHistory.TYPE_USER: 1,
        UserHistory.TYPE_SONG: 2,
        UserHistory.TYPE_ALBUM: 3,
        UserHistory.TYPE_PLAYLIST: 4,
        UserHistory.TYPE_ARTIST: 5,
    }
    lookup = {'user': user, 'content_type': content_type, **target}
    now = timezone.now()

    with transaction.atomic():
        if connection.vendor == 'postgresql':
            first_key = int(user.pk) % 2147483647
            second_key = ((type_codes.get(content_type, 0) << 24) ^ int(target_id)) % 2147483647
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(%s, %s)', [first_key, second_key])

        rows = UserHistory.objects.select_for_update().filter(**lookup).order_by('-updated_at', '-id')
        current = rows.first()
        if current is None:
            return UserHistory.objects.create(**lookup)

        rows.exclude(pk=current.pk).delete()
        UserHistory.objects.filter(pk=current.pk).update(updated_at=now)
        current.updated_at = now
        return current


def _history_queryset(user):
    # History rows can outlive a deleted target because several relations use
    # SET_NULL. Exclude those orphaned rows before pagination/serialization so
    # clients never receive ``item: null`` entries.
    valid_target = (
        Q(content_type=UserHistory.TYPE_SONG, song__isnull=False)
        | Q(content_type=UserHistory.TYPE_ALBUM, album__isnull=False)
        | Q(content_type=UserHistory.TYPE_PLAYLIST, playlist__isnull=False)
        | Q(content_type=UserHistory.TYPE_ARTIST, artist__isnull=False)
        | Q(content_type=UserHistory.TYPE_USER, target_user__isnull=False)
    )
    return UserHistory.objects.filter(user=user).filter(valid_target).select_related(
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

        response = Response(data)
        response['Cache-Control'] = 'private, no-store, max-age=0'
        response['Pragma'] = 'no-cache'
        response['Vary'] = 'Authorization, Accept-Language'
        return response

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
        with transaction.atomic():
            setting, _ = NotificationSetting.objects.get_or_create(user=request.user)
            setting = NotificationSetting.objects.select_for_update().get(pk=setting.pk)
            serializer = NotificationSettingSerializer(setting, data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)

    @extend_schema(
        summary="به‌روزرسانی تنظیمات اعلان‌ها (جزئی)",
        description="تغییر برخی از تنظیمات اعلان‌های کاربر.",
        request=NotificationSettingSerializer,
        responses={200: NotificationSettingSerializer}
    )
    def patch(self, request):
        with transaction.atomic():
            setting, _ = NotificationSetting.objects.get_or_create(user=request.user)
            setting = NotificationSetting.objects.select_for_update().get(pk=setting.pk)
            serializer = NotificationSettingSerializer(setting, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)


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
                    'action': serializers.CharField(),
                    'message': serializers.CharField(),
                    'is_following': serializers.BooleanField(),
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
        desired_state = serializer.validated_data.get('follow')

        follower = request.user
        # If the user has an artist profile, we could potentially follow as an artist.
        # For now, we follow as the User account as per "users only can post to it".
        # But we'll check if they want to follow as artist if we add that later.

        if user_id:
            target = get_object_or_404(User, id=user_id)
            if target == follower:
                return Response({'error': 'You cannot follow yourself.'}, status=status.HTTP_400_BAD_REQUEST)

            follow_qs = Follow.objects.filter(follower_user=follower, followed_user=target)
            currently_following = follow_qs.exists()
            should_follow = (not currently_following) if desired_state is None else desired_state

            if should_follow:
                Follow.objects.get_or_create(follower_user=follower, followed_user=target)
            else:
                follow_qs.delete()

            action = 'followed' if should_follow else 'unfollowed'
            return Response({
                'status': 'ok',
                'action': action,
                'message': action,
                'is_following': should_follow,
            }, status=status.HTTP_200_OK)

        if artist_id:
            target = get_object_or_404(Artist, id=artist_id)
            follow_qs = Follow.objects.filter(follower_user=follower, followed_artist=target)
            currently_following = follow_qs.exists()
            should_follow = (not currently_following) if desired_state is None else desired_state

            if should_follow:
                Follow.objects.get_or_create(follower_user=follower, followed_artist=target)
            else:
                follow_qs.delete()

            action = 'followed' if should_follow else 'unfollowed'
            return Response({
                'status': 'ok',
                'action': action,
                'message': action,
                'is_following': should_follow,
            }, status=status.HTTP_200_OK)


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


DOWNLOAD_QUALITY_CACHE_TTL = 60 * 60 * 24 * 365


def _download_quality_cache_key(user_id, song_id):
    return f"download-quality:v1:{int(user_id)}:{int(song_id)}"


def _download_quality_map(user_id, song_ids):
    song_ids = [int(song_id) for song_id in song_ids]
    keys = [_download_quality_cache_key(user_id, song_id) for song_id in song_ids]
    cached = cache.get_many(keys) if keys else {}
    return {
        song_id: cached.get(_download_quality_cache_key(user_id, song_id))
        for song_id in song_ids
    }


def _signed_download_url(raw_url, expiration=900):
    if not raw_url:
        return None
    cdn_base = getattr(settings, 'R2_CDN_BASE', '').rstrip('/')
    from urllib.parse import unquote, urlparse
    if cdn_base and raw_url.startswith(cdn_base + '/'):
        object_key = unquote(raw_url[len(cdn_base) + 1:])
        return generate_signed_r2_url(object_key, expiration=expiration) or raw_url
    parsed = urlparse(raw_url)
    if parsed.scheme in {'http', 'https'}:
        return raw_url
    return generate_signed_r2_url(unquote(parsed.path.lstrip('/')), expiration=expiration) or raw_url


@extend_schema(tags=['Library Page Endpoints اندپوینت های صفحه کتابخانه'])
class DownloadHistoryView(generics.ListAPIView):
    """List completed browser downloads and record the latest chosen quality."""
    serializer_class = DownloadHistorySerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination

    @extend_schema(
        summary="دریافت تاریخچه دانلودهای کاربر",
        description="دریافت لیست آهنگ‌های دانلودشده همراه با آخرین کیفیت دانلود.",
        responses={200: DownloadHistorySerializer(many=True)}
    )
    def get_queryset(self):
        return (
            DownloadHistory.objects
            .filter(user=self.request.user)
            .select_related('song', 'song__artist', 'song__album')
            .prefetch_related(
                'song__featured_artists', 'song__genres', 'song__tags',
                'song__moods', 'song__sub_genres',
            )
            .order_by('-updated_at')
        )

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        items = list(page if page is not None else queryset)
        quality_map = _download_quality_map(request.user.id, [item.song_id for item in items])
        context = {**self.get_serializer_context(), 'download_quality_map': quality_map}
        data = self.get_serializer(items, many=True, context=context).data
        return self.get_paginated_response(data) if page is not None else Response(data)

    @extend_schema(
        summary="ثبت دانلود تکمیل‌شده آهنگ",
        description="پس از آماده‌شدن کامل Blob در مرورگر، آهنگ و کیفیت انتخاب‌شده را در تاریخچه ثبت می‌کند.",
        request=inline_serializer(
            name='DownloadRequest',
            fields={
                'song_id': serializers.IntegerField(),
                'quality': serializers.ChoiceField(choices=['128', '320']),
            }
        ),
        responses={201: DownloadHistorySerializer, 200: DownloadHistorySerializer}
    )
    def post(self, request, *args, **kwargs):
        song_id = request.data.get('song_id')
        quality = str(request.data.get('quality') or '')
        if not song_id:
            return Response({'error': {'code': 'SONG_ID_REQUIRED', 'message': 'song_id is required'}}, status=status.HTTP_400_BAD_REQUEST)
        if quality not in {'128', '320'}:
            return Response({'error': {'code': 'DOWNLOAD_QUALITY_INVALID', 'message': 'quality must be 128 or 320'}}, status=status.HTTP_400_BAD_REQUEST)

        song = get_object_or_404(Song, id=song_id, status=Song.STATUS_PUBLISHED)
        obj, created = DownloadHistory.objects.update_or_create(
            user=request.user,
            song=song,
            defaults={'updated_at': timezone.now()}
        )
        cache.set(
            _download_quality_cache_key(request.user.id, song.id),
            quality,
            timeout=DOWNLOAD_QUALITY_CACHE_TTL,
        )
        context = {
            **self.get_serializer_context(),
            'download_quality_map': {song.id: quality},
        }
        serializer = self.get_serializer(obj, context=context)
        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class SongPlaybackQualityView(APIView):
    """Provide a replacement source for the currently playing song without creating a new play session."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary='تغییر کیفیت آهنگ در حال پخش',
        request=inline_serializer(
            name='SongPlaybackQualityRequest',
            fields={'quality': serializers.ChoiceField(choices=['medium', 'high'])},
        ),
        responses={200: inline_serializer(
            name='SongPlaybackQualityResponse',
            fields={
                'song_id': serializers.IntegerField(),
                'quality': serializers.CharField(),
                'stream_url': serializers.URLField(),
                'expires_in': serializers.IntegerField(),
            },
        )},
    )
    def post(self, request, pk):
        song = get_object_or_404(Song, pk=pk, status=Song.STATUS_PUBLISHED)
        quality = str(request.data.get('quality') or '')
        if quality not in {'medium', 'high'}:
            return Response({'error': {'code': 'PLAYBACK_QUALITY_INVALID', 'message': 'quality must be medium or high'}}, status=status.HTTP_400_BAD_REQUEST)
        if quality == 'high' and request.user.plan != User.PLAN_PREMIUM:
            return Response({'error': {'code': 'PREMIUM_REQUIRED', 'message': 'High quality is available to Premium users.'}}, status=status.HTTP_403_FORBIDDEN)

        raw_url = song.converted_audio_url if quality == 'medium' else song.audio_file
        if not raw_url:
            return Response({'error': {'code': 'PLAYBACK_QUALITY_UNAVAILABLE', 'message': 'This quality is unavailable for the current song.'}}, status=status.HTTP_409_CONFLICT)

        signed_url = _signed_download_url(raw_url, expiration=3600)
        if not signed_url:
            return Response({'error': {'code': 'PLAYBACK_SOURCE_UNAVAILABLE', 'message': 'The playback source is unavailable.'}}, status=status.HTTP_409_CONFLICT)

        response = Response({
            'song_id': song.id,
            'quality': quality,
            'stream_url': signed_url,
            'expires_in': 3600,
        })
        response['Cache-Control'] = 'private, no-store'
        return response


def _download_filename(song, quality):
    safe_title = re.sub(r'[<>:"/\\|?*]+', '', song.display_title or song.title).strip() or f'song-{song.id}'
    safe_artist = re.sub(r'[<>:"/\\|?*]+', '', artist_filename_name(song.artist)).strip()
    return f"{safe_title}{' - ' + safe_artist if safe_artist else ''} [{quality}kbps].mp3"


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class SongDownloadView(APIView):
    """Return available download qualities and short-lived direct URLs."""
    permission_classes = [IsAuthenticated]

    def _song(self, request, pk):
        queryset = Song.objects.select_related('artist')
        if not request.user.is_staff:
            queryset = queryset.filter(status=Song.STATUS_PUBLISHED)
        return get_object_or_404(queryset, pk=pk)

    def _options(self, request, song):
        is_premium = request.user.plan == User.PLAN_PREMIUM
        return [
            {
                'quality': '128',
                'label': '128 kbps',
                'available': bool(is_premium and song.converted_audio_url),
                'requires_premium': True,
                'reason': None if is_premium and song.converted_audio_url else (
                    'PREMIUM_REQUIRED' if not is_premium else 'SOURCE_UNAVAILABLE'
                ),
            },
            {
                'quality': '320',
                'label': '320 kbps',
                'available': bool(is_premium and song.audio_file),
                'requires_premium': True,
                'reason': None if is_premium and song.audio_file else (
                    'PREMIUM_REQUIRED' if not is_premium else 'SOURCE_UNAVAILABLE'
                ),
            },
        ]

    @extend_schema(
        summary='دریافت کیفیت‌های قابل دانلود',
        responses={200: inline_serializer(
            name='SongDownloadOptionsResponse',
            fields={
                'song_id': serializers.IntegerField(),
                'can_download': serializers.BooleanField(),
                'qualities': serializers.ListField(child=serializers.DictField()),
            },
        )},
    )
    def get(self, request, pk):
        song = self._song(request, pk)
        options = self._options(request, song)
        response = Response({
            'song_id': song.id,
            'can_download': any(option['available'] for option in options),
            'qualities': options,
        })
        response['Cache-Control'] = 'private, no-store'
        return response

    @extend_schema(
        summary='آماده‌سازی لینک مستقیم دانلود',
        request=inline_serializer(
            name='SongDownloadPrepareRequest',
            fields={'quality': serializers.ChoiceField(choices=['128', '320'])},
        ),
        responses={200: inline_serializer(
            name='SongDownloadPrepareResponse',
            fields={
                'song_id': serializers.IntegerField(),
                'quality': serializers.CharField(),
                'download_url': serializers.URLField(),
                'proxy_url': serializers.URLField(),
                'filename': serializers.CharField(),
                'expires_in': serializers.IntegerField(),
            },
        )},
    )
    def post(self, request, pk):
        song = self._song(request, pk)
        quality = str(request.data.get('quality') or '')
        options = {option['quality']: option for option in self._options(request, song)}
        option = options.get(quality)
        if option is None:
            return Response({'error': {'code': 'DOWNLOAD_QUALITY_INVALID', 'message': 'quality must be 128 or 320'}}, status=status.HTTP_400_BAD_REQUEST)
        if not option['available']:
            code = option['reason'] or 'DOWNLOAD_QUALITY_UNAVAILABLE'
            http_status = status.HTTP_403_FORBIDDEN if code == 'PREMIUM_REQUIRED' else status.HTTP_409_CONFLICT
            return Response({'error': {'code': code, 'message': 'The selected download quality is unavailable.'}}, status=http_status)

        raw_url = song.converted_audio_url if quality == '128' else song.audio_file
        signed_url = _signed_download_url(raw_url, expiration=900)
        if not signed_url:
            return Response({'error': {'code': 'DOWNLOAD_SOURCE_UNAVAILABLE', 'message': 'The download source is unavailable.'}}, status=status.HTTP_409_CONFLICT)

        filename = _download_filename(song, quality)
        proxy_path = reverse('song_download_file', kwargs={'pk': song.id})
        proxy_url = request.build_absolute_uri(f"{proxy_path}?quality={quality}")
        response = Response({
            'song_id': song.id,
            'quality': quality,
            'download_url': signed_url,
            'proxy_url': proxy_url,
            'filename': filename,
            'expires_in': 900,
        })
        response['Cache-Control'] = 'private, no-store'
        return response


@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class SongDownloadFileView(SongDownloadView):
    """Stream a download through the API only when direct object-storage CORS is unavailable."""
    http_method_names = ['get', 'head', 'options']

    def get(self, request, pk):
        song = self._song(request, pk)
        quality = str(request.query_params.get('quality') or '')
        options = {option['quality']: option for option in self._options(request, song)}
        option = options.get(quality)
        if option is None:
            return Response({'error': {'code': 'DOWNLOAD_QUALITY_INVALID', 'message': 'quality must be 128 or 320'}}, status=status.HTTP_400_BAD_REQUEST)
        if not option['available']:
            code = option['reason'] or 'DOWNLOAD_QUALITY_UNAVAILABLE'
            http_status = status.HTTP_403_FORBIDDEN if code == 'PREMIUM_REQUIRED' else status.HTTP_409_CONFLICT
            return Response({'error': {'code': code, 'message': 'The selected download quality is unavailable.'}}, status=http_status)

        raw_url = song.converted_audio_url if quality == '128' else song.audio_file
        signed_url = _signed_download_url(raw_url, expiration=900)
        if not signed_url:
            return Response({'error': {'code': 'DOWNLOAD_SOURCE_UNAVAILABLE', 'message': 'The download source is unavailable.'}}, status=status.HTTP_409_CONFLICT)

        try:
            upstream = requests.get(signed_url, stream=True, timeout=(5, 30))
            upstream.raise_for_status()
        except requests.RequestException:
            return Response({'error': {'code': 'DOWNLOAD_SOURCE_UNAVAILABLE', 'message': 'The download source is unavailable.'}}, status=status.HTTP_502_BAD_GATEWAY)

        def stream_chunks():
            try:
                for chunk in upstream.iter_content(chunk_size=256 * 1024):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        response = StreamingHttpResponse(
            stream_chunks(),
            content_type=upstream.headers.get('Content-Type') or 'audio/mpeg',
        )
        content_length = upstream.headers.get('Content-Length')
        if content_length:
            response['Content-Length'] = content_length
        from urllib.parse import quote
        filename = _download_filename(song, quality)
        ascii_filename = re.sub(r'[^A-Za-z0-9._ -]+', '_', filename) or f'song-{song.id}-{quality}.mp3'
        response['Content-Disposition'] = (
            f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{quote(filename)}'
        )
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response


@extend_schema(tags=['Library Page Endpoints اندپوینت های صفحه کتابخانه'])
class DownloadHistoryDeleteView(generics.DestroyAPIView):
    """
    Delete a single download history entry for the authenticated user.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = DownloadHistorySerializer

    def get_queryset(self):
        return DownloadHistory.objects.filter(user=self.request.user)

    def perform_destroy(self, instance):
        cache.delete(_download_quality_cache_key(instance.user_id, instance.song_id))
        instance.delete()

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
            featured_names = [artist_filename_name(a) for a in featured_artists]

            artist_name = artist_filename_name(artist)
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

        serializer = ArtistSerializer(qs, many=True, context={'request': request, 'artist_panel': str(request.query_params.get('artist_panel') or '').lower() in {'1', 'true', 'yes', 'on'}})
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
        ).filter(pk=pk, songs__status=Song.STATUS_PUBLISHED).distinct().first()
        if not playlist: return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if request.user.is_authenticated:
            _touch_user_history(request.user, UserHistory.TYPE_PLAYLIST, playlist=playlist)
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
            _touch_user_history(request.user, UserHistory.TYPE_ARTIST, artist=artist)
        page, page_size = _page_values(request, 10, 50); offset = (page - 1) * page_size
        song_base = _song_card_queryset().filter(artist=artist)
        top = song_base.annotate(total_plays=Coalesce(F('plays'), 0) + Count('play_counts')).order_by('-total_plays', '-created_at')
        latest = song_base.order_by('-release_date', '-created_at')
        albums = Album.objects.filter(artist=artist, songs__status=Song.STATUS_PUBLISHED).exclude(Q(title__iexact='single') | Q(title='سینگل')).distinct().select_related('artist').prefetch_related(
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
        albums = Album.objects.filter(songs__status=Song.STATUS_PUBLISHED).distinct().select_related('artist').prefetch_related(
            'genres', 'sub_genres', 'moods',
            Prefetch('songs', queryset=_song_card_queryset(), to_attr='_detail_songs'),
        )
        serializer = AlbumSerializer(albums, many=True, context={'request': request})
        return Response(serializer.data)




@extend_schema(tags=['Utility , DetailScreens & action Endpoints اندپوینت های ابزار و صفحات جزئیات و عملیات'])
class AlbumDetailView(APIView):
    def get_permissions(self): return [AllowAny()] if self.request.method == 'GET' else [IsAuthenticated()]

    def get(self, request, pk):
        album = Album.objects.select_related('artist').prefetch_related(
            'genres', 'sub_genres', 'moods', Prefetch('songs', queryset=_song_card_queryset(), to_attr='_detail_songs')
        ).filter(pk=pk, songs__status=Song.STATUS_PUBLISHED).distinct().first()
        if not album: return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if request.user.is_authenticated:
            _touch_user_history(request.user, UserHistory.TYPE_ALBUM, album=album)
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
            _touch_user_history(request.user, UserHistory.TYPE_SONG, song=song)
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
        _touch_user_history(request.user, UserHistory.TYPE_SONG, song=song)

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
            pay_value = Decimal('0.00000000')
            if config:
                if request.user.plan == User.PLAN_PREMIUM:
                    pay_value = config.premium_play_worth
                else:
                    pay_value = config.free_play_worth
            pay_value = _finance_decimal(pay_value)

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
        # Canonical public links use unique_id. Named canonical routes and old
        # numeric links may explicitly request a database-primary-key lookup.
        # Keep both forms supported so navigation cannot strand a valid user.
        lookup_mode = str(request.query_params.get('lookup', '')).strip().lower()
        users = User.objects.select_related('image_profile')

        if lookup_mode in {'pk', 'id'}:
            try:
                user_pk = int(str(unique_id).strip())
            except (TypeError, ValueError):
                raise Http404('User not found')
            user = get_object_or_404(users, pk=user_pk)
        else:
            user = users.filter(unique_id=unique_id).first()
            # Backward compatibility for legacy /user/{numeric-id} links.
            # An explicit unique_id match always wins, so numeric public UIDs
            # continue to work correctly.
            if user is None and str(unique_id).strip().isdigit():
                user = users.filter(pk=int(str(unique_id).strip())).first()
            if user is None:
                raise Http404('User not found')

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
            _touch_user_history(request.user, UserHistory.TYPE_USER, target_user=user)

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


def _sedabox_preview_payload(request, user, page_size=10):
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
    persisted_ids = {
        str(value)
        for value in _home_playlist_queryset(request.user).values_list('unique_id', flat=True)
        if value is not None
    }
    generated_ids = {
        str(item.unique_id)
        for item in generated_all
        if getattr(item, 'unique_id', None) is not None
    }
    total = normal_qs.count() + len(persisted_ids | generated_ids)
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
                page_size = max(1, min(int(request.query_params.get('page_size', 10)), 10))
            except (TypeError, ValueError):
                page_size = 10
            key = stable_cache_key(
                'sedabox-profile-preview', get_request_language(request), not request.user.is_authenticated, page_size,
                cache_version(CATALOG_VERSION_KEY), cache_version(USER_DIRECTORY_VERSION_KEY), 'v4',
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
            _touch_user_history(request.user, UserHistory.TYPE_USER, target_user=user)

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

        # Generated recommendations can also exist in the persisted recommended
        # queryset. Count unique public identities so pagination never advertises
        # phantom pages or stops after the first 20 visible records.
        generated_ids = {
            str(item.unique_id)
            for item in generated
            if getattr(item, 'unique_id', None) is not None
        }
        persisted_recommended_ids = set(
            str(value)
            for value in recommended_qs.values_list('unique_id', flat=True)
            if value is not None
        )
        recommended_unique_total = len(generated_ids | persisted_recommended_ids)
        total = normal_total + recommended_unique_total
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
    # Cheap, cache-guarded publication sweep keeps scheduled releases automatic
    # without changing any audience endpoint or requiring a client update.
    try:
        if cache.add('artist-release-due-publish-lock', '1', timeout=60):
            from .release_service import publish_due_releases
            publish_due_releases(limit=25)
    except Exception:
        # Deployment may briefly run before the additive release tables exist.
        pass
    qs = _song_card_queryset()
    if require_preview:
        qs = qs.filter(preview_audio_url__isnull=False).exclude(preview_audio_url='')
    return qs


def _home_album_queryset():
    song_qs = _home_song_queryset().order_by('-release_date', '-created_at')
    return Album.objects.filter(songs__status=Song.STATUS_PUBLISHED).distinct().select_related('artist').prefetch_related(
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
    key = stable_cache_key('fresh-playlist-recipes', require_preview, bucket, version, 'v7')
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
            f'{generated_term_en(genre.get("name"), genre.get("name_en"), generic="Genre")} Wave',
            f'یک میکس تازه از فضای {genre["name"]}',
            f'A fresh mix inspired by {generated_term_en(genre.get("name"), genre.get("name_en"), generic="this genre")}',
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
            f'{generated_term_en(mood.get("name"), mood.get("name_en"), generic="A Mood")} for This Moment',
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
    key = stable_cache_key('user-music-activity', user.pk, user_affinity_version(user.pk), 'v2')
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


def _ensure_personal_recommendations(user, target=18):
    """Maintain a compact, current personal playlist pool for one user.

    The expensive factor analysis runs at most once per five-minute bucket and
    again immediately when that user's affinity version changes. Rows are
    created in bulk and old unused generations are removed by Redis maintenance.
    """
    if user is None or not getattr(user, 'is_authenticated', False):
        return []
    if not _user_has_music_activity(user):
        return []

    affinity = user_affinity_version(user.pk)
    bucket = _time_bucket(15)

    # Repair old server-generated fallback titles such as "Fresh for You 5".
    # This is deliberately narrow so user-authored or editorial titles ending
    # in a number are never modified.
    title_cleanup_key = stable_cache_key(
        'normalize-personal-playlist-fallback-titles', user.pk, 'v1'
    )
    if cache_get(title_cleanup_key) is None:
        fallback_titles_fa = ('تازه برای شما', 'محبوب برای شما', 'کشف بعدی شما')
        fallback_titles_en = ('Fresh for You', 'Popular for You', 'Your Next Discovery')

        def strip_legacy_suffix(value, allowed_titles):
            text = str(value or '').strip()
            for allowed_title in allowed_titles:
                if re.fullmatch(rf'{re.escape(allowed_title)}\s+[0-9]+', text):
                    return allowed_title
            return text

        repaired = []
        legacy_rows = RecommendedPlaylist.objects.filter(user=user).only(
            'id', 'title', 'title_en'
        )
        for item in legacy_rows.iterator(chunk_size=100):
            clean_title = strip_legacy_suffix(item.title, fallback_titles_fa)
            clean_title_en = strip_legacy_suffix(item.title_en, fallback_titles_en)
            if clean_title == item.title and clean_title_en == item.title_en:
                continue
            item.title = clean_title
            item.title_en = clean_title_en
            repaired.append(item)
        if repaired:
            RecommendedPlaylist.objects.bulk_update(
                repaired, ['title', 'title_en'], batch_size=100
            )
        cache_set(title_cleanup_key, True, 24 * 60 * 60)

    generation_key = stable_cache_key(
        'ensure-personal-playlist-pool', user.pk, affinity, bucket, target, 'v9'
    )
    cached, claimed = cache_get_or_claim(generation_key, lock_timeout=30, wait_timeout=0.6)
    if cached is not None:
        return list(cached)

    interacted = Song.objects.filter(
        Q(liked_by=user) | Q(play_counts__user=user) | Q(user_playlists__user=user),
        status=Song.STATUS_PUBLISHED,
    ).distinct()
    genre_ids = list(
        interacted.exclude(genres__id=None)
        .values('genres__id').annotate(n=Count('id')).order_by('-n')
        .values_list('genres__id', flat=True)[:4]
    )
    mood_ids = list(
        interacted.exclude(moods__id=None)
        .values('moods__id').annotate(n=Count('id')).order_by('-n')
        .values_list('moods__id', flat=True)[:3]
    )
    artist_ids = list(
        interacted.values('artist_id').annotate(n=Count('id')).order_by('-n')
        .values_list('artist_id', flat=True)[:3]
    )

    genre_rows = {
        row['id']: row for row in Genre.objects.filter(id__in=genre_ids)
        .values('id', 'name', 'name_en', 'slug')
    }
    mood_rows = {
        row['id']: row for row in Mood.objects.filter(id__in=mood_ids)
        .values('id', 'name', 'name_en', 'slug')
    }
    artist_rows = {
        row['id']: row for row in Artist.objects.filter(id__in=artist_ids)
        .values('id', 'name', 'name_en')
    }

    configs = []
    for value in genre_ids:
        configs.extend([('genre', value, 1), ('genre', value, 2)])
    for value in mood_ids:
        configs.append(('mood', value, 1))
    for value in artist_ids:
        configs.append(('artist', value, 1))
    configs.append(('blend', 0, 1))

    base = _home_song_queryset()
    used_song_ids = set()
    seen_song_sets = set()
    seen_song_orders = set()
    recipes = []
    now = timezone.now()

    def register_song_order(song_ids):
        ordered_ids = list(dict.fromkeys(song_ids))
        if len(ordered_ids) < 3:
            return []
        order_signature = tuple(ordered_ids)
        if order_signature in seen_song_orders:
            return []
        seen_song_orders.add(order_signature)
        seen_song_sets.add(frozenset(ordered_ids))
        used_song_ids.update(ordered_ids)
        return ordered_ids

    def pick(queryset, size, seed):
        """Build a recommendation distinct by content, or at least by order.

        Duplicate playlist titles are harmless and intentionally allowed. Exact
        duplicate queues are not: we first try several different song sets, then
        accept the same set only when its order is new. If the available catalog
        cannot produce another distinct order, no redundant playlist is created.
        """
        candidates = list(dict.fromkeys(
            queryset.values_list('id', flat=True)[:120]
        ))
        if len(candidates) < 3:
            return []

        selection_size = min(size, len(candidates))
        distinct_content = []
        reordered_content = []
        local_orders = set()

        def consider(song_ids):
            ordered_ids = list(dict.fromkeys(song_ids))[:selection_size]
            if len(ordered_ids) < 3:
                return
            order_signature = tuple(ordered_ids)
            if order_signature in local_orders or order_signature in seen_song_orders:
                return
            local_orders.add(order_signature)
            destination = (
                distinct_content
                if frozenset(ordered_ids) not in seen_song_sets
                else reordered_content
            )
            destination.append(ordered_ids)

        # Prefer unused songs while enough catalog depth exists. Later attempts
        # deliberately relax that preference to find a distinct set/order for
        # narrow genres, moods, artists, or small catalogs.
        for attempt in range(24):
            shuffled = list(candidates)
            random.Random(f'{seed}:attempt:{attempt}').shuffle(shuffled)
            if attempt < 12:
                shuffled = (
                    [song_id for song_id in shuffled if song_id not in used_song_ids]
                    + [song_id for song_id in shuffled if song_id in used_song_ids]
                )
            consider(shuffled)

        # Deterministic rotations guarantee useful order-only alternatives when
        # every available playlist must contain the same small song set.
        base_order = list(candidates[:selection_size])
        for reverse in (False, True):
            ordered = list(reversed(base_order)) if reverse else base_order
            for offset in range(len(ordered)):
                consider(ordered[offset:] + ordered[:offset])

        pool = distinct_content or reordered_content
        if not pool:
            return []

        def diversity_score(song_ids):
            song_set = set(song_ids)
            unseen_count = len(song_set - used_song_ids)
            if not seen_song_sets:
                nearest_distance = len(song_set)
            else:
                nearest_distance = min(
                    len(song_set.symmetric_difference(existing_set))
                    for existing_set in seen_song_sets
                )
            return unseen_count, nearest_distance

        return register_song_order(max(pool, key=diversity_score))

    for kind, value, variant in configs:
        if len(recipes) >= target:
            break
        if kind == 'genre':
            row = genre_rows.get(value, {})
            fa = row.get('name') or 'سبک محبوب شما'
            en = generated_term_en(row.get('name'), row.get('name_en'), generic='Your Favorite Genre')
            title = f'میکس {fa}' if variant == 1 else f'کشف {fa}'
            title_en = f'{en} Mix' if variant == 1 else f'Discover {en}'
            description = 'منتخب تازه براساس سبک‌های مورد علاقه شما'
            description_en = 'A fresh selection based on the genres you enjoy most'
            queryset = base.filter(genres__id=value).distinct().order_by('-plays', '-release_date', '-created_at')
            playlist_type = RecommendedPlaylist.PLAYLIST_TYPE_DISCOVER_GENRE
        elif kind == 'mood':
            row = mood_rows.get(value, {})
            fa = row.get('name') or 'حال‌وهوای شما'
            en = generated_term_en(row.get('name'), row.get('name_en'), generic='Your Mood')
            title, title_en = f'حال‌وهوای {fa}', f'{en} Mood'
            description = 'یک جریان تازه متناسب با حال‌وهوای شنیداری شما'
            description_en = 'A fresh flow matched to your listening mood'
            queryset = base.filter(moods__id=value).distinct().order_by('-plays', '-release_date', '-created_at')
            playlist_type = RecommendedPlaylist.PLAYLIST_TYPE_MOOD_BASED
        elif kind == 'artist':
            row = artist_rows.get(value, {})
            fa = row.get('name') or 'هنرمند محبوب شما'
            en = generated_term_en(row.get('name'), row.get('name_en'), generic='Your Favorite Artist')
            title, title_en = f'منتخب {fa}', f'{en} Essentials'
            description = 'ترک‌های برتر از هنرمندانی که بیشتر دنبال می‌کنید'
            description_en = 'Top tracks from artists closest to your current taste'
            queryset = base.filter(artist_id=value).order_by('-plays', '-release_date', '-created_at')
            playlist_type = RecommendedPlaylist.PLAYLIST_TYPE_ARTIST_MIX
        else:
            title, title_en = 'برای امروز شما', 'Made for You Today'
            description = 'ترکیبی تازه از سبک‌ها، مودها و هنرمندان محبوب شما'
            description_en = 'A fresh blend of your favorite genres, moods, and artists'
            factor_filter = Q()
            if genre_ids:
                factor_filter |= Q(genres__id__in=genre_ids)
            if mood_ids:
                factor_filter |= Q(moods__id__in=mood_ids)
            if artist_ids:
                factor_filter |= Q(artist_id__in=artist_ids)
            queryset = (
                base.filter(factor_filter).distinct() if factor_filter else base
            ).order_by('-plays', '-release_date', '-created_at')
            playlist_type = RecommendedPlaylist.PLAYLIST_TYPE_SIMILAR_TASTE

        song_ids = pick(queryset, 18, f'{user.pk}:{affinity}:{bucket}:{kind}:{value}:{variant}')
        if len(song_ids) < 3:
            continue
        recipes.append({
            'title': title,
            'title_en': title_en,
            'description': description,
            'description_en': description_en,
            'playlist_type': playlist_type,
            'song_ids': song_ids,
        })

    # Sparse profiles still receive a complete pool from strong catalog signals.
    fallback_specs = [
        ('تازه برای شما', 'Fresh for You', '-release_date'),
        ('محبوب برای شما', 'Popular for You', '-plays'),
        ('کشف بعدی شما', 'Your Next Discovery', '-created_at'),
        ('برای امروز شما', 'Made for You Today', '-plays'),
        ('جریان روزانه شما', 'Your Daily Flow', '-release_date'),
        ('انتخاب‌های شما', 'Your Picks', '-created_at'),
    ]
    fallback_index = 0
    consecutive_misses = 0
    max_fallback_attempts = max(target * 6, 36)
    while len(recipes) < target and fallback_index < max_fallback_attempts:
        fa_title, en_title, ordering = fallback_specs[fallback_index % len(fallback_specs)]
        queryset = base.order_by(ordering, '-release_date', '-created_at')
        song_ids = pick(
            queryset, 18,
            f'{user.pk}:{affinity}:{bucket}:fallback:{fallback_index}',
        )
        fallback_index += 1
        if len(song_ids) < 3:
            consecutive_misses += 1
            # The available catalog has exhausted every distinct song order.
            # Stop instead of creating an exact duplicate playlist.
            if consecutive_misses >= len(fallback_specs) * 2:
                break
            continue
        consecutive_misses = 0
        recipes.append({
            # Repeated human-readable names are intentional. Diversity belongs
            # in the queue, never in ugly numeric suffixes such as "For You 5".
            'title': fa_title,
            'title_en': en_title,
            'description': 'پیشنهاد تازه براساس سلیقه و شنیده‌های فعلی شما',
            'description_en': 'A fresh recommendation based on your current taste and listening history',
            'playlist_type': RecommendedPlaylist.PLAYLIST_TYPE_SIMILAR_TASTE,
            'song_ids': song_ids,
        })

    if not recipes:
        cache_set(generation_key, [], 60)
        return []

    unique_ids = [
        f'smart_rec_{user.pk}_{affinity}_{bucket}_{index}'
        for index in range(1, len(recipes) + 1)
    ]
    existing_qs = RecommendedPlaylist.objects.filter(unique_id__in=unique_ids)
    existing = {item.unique_id: item for item in existing_qs}
    durable_ids = set(
        existing_qs.filter(
            Q(expires_at__isnull=True) | Q(views__gt=0)
            | Q(liked_by__isnull=False) | Q(saved_by__isnull=False)
            | Q(viewed_by__isnull=False)
        ).values_list('id', flat=True).distinct()
    )
    to_create = []
    to_update = []
    for index, (unique_id, recipe) in enumerate(zip(unique_ids, recipes), 1):
        defaults = {
            'user': user,
            'title': recipe['title'],
            'title_en': recipe['title_en'],
            'description': recipe['description'],
            'description_en': recipe['description_en'],
            'playlist_type': recipe['playlist_type'],
            'song_order': recipe['song_ids'],
            'relevance_score': 110 - index,
            'match_percentage': max(78, 98 - index),
            'expires_at': now + timedelta(hours=2),
            'updated_at': now,
        }
        item = existing.get(unique_id)
        if item is None:
            to_create.append(RecommendedPlaylist(
                unique_id=unique_id, created_at=now, **defaults
            ))
        elif item.pk not in durable_ids:
            for field, value in defaults.items():
                setattr(item, field, value)
            to_update.append(item)

    if to_create:
        RecommendedPlaylist.objects.bulk_create(to_create, ignore_conflicts=True, batch_size=100)
    if to_update:
        RecommendedPlaylist.objects.bulk_update(
            to_update,
            ['user', 'title', 'title_en', 'description', 'description_en',
             'playlist_type', 'song_order', 'relevance_score', 'match_percentage',
             'expires_at', 'updated_at'],
            batch_size=100,
        )

    stored = list(RecommendedPlaylist.objects.filter(unique_id__in=unique_ids))
    stored_by_uid = {item.unique_id: item for item in stored}
    through = RecommendedPlaylist.songs.through
    mutable_ids = [item.pk for item in stored if item.pk not in durable_ids]
    if mutable_ids:
        through.objects.filter(recommendedplaylist_id__in=mutable_ids).delete()
        links = []
        for unique_id, recipe in zip(unique_ids, recipes):
            item = stored_by_uid.get(unique_id)
            if item is None or item.pk in durable_ids:
                continue
            links.extend(
                through(recommendedplaylist_id=item.pk, song_id=song_id)
                for song_id in recipe['song_ids']
            )
        through.objects.bulk_create(links, ignore_conflicts=True, batch_size=1000)

    cache_set(generation_key, unique_ids, 5 * 60)
    return unique_ids


def _playlist_recommendation_items(user=None, limit=80):
    authenticated = bool(user is not None and getattr(user, 'is_authenticated', False))
    base = _home_playlist_queryset(user)
    dynamic = _dynamic_playlist_items(user)
    if authenticated:
        # Expiring personal rows are useful only while they are current. Durable
        # rows (liked/saved/detail-viewed copies) have no expiry and stay visible.
        personal = list(
            base.filter(user=user).filter(
                Q(expires_at__isnull=True)
                | Q(updated_at__gte=timezone.now() - timedelta(minutes=20))
            ).order_by('-relevance_score', '-created_at')[:limit]
        )
        global_items = list(
            base.filter(user__isnull=True).order_by('-relevance_score', '-created_at')[:limit]
        )
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

    if authenticated:
        items = fresh_order_objects(
            f'recommended-playlists:user:{user.pk}', items,
            identity=lambda item: item.unique_id,
        )
    return items[:limit]


def _remember_playlist_results(user, items):
    items = list(items)
    if user is not None and getattr(user, 'is_authenticated', False):
        remember_exposure(
            f'recommended-playlists:user:{user.pk}',
            [item.unique_id for item in items],
            recent_window=160,
        )
    mark_generated_playlist_usage(items)


def _dynamic_playlist_by_unique_id(user, unique_id):
    match = re.fullmatch(r'freshmix_(\d+)_([a-z0-9]+)', unique_id or '')
    if not match:
        return None
    bucket = int(match.group(1))
    return next((item for item in _dynamic_playlist_items(user, bucket) if item.unique_id == unique_id), None)


def _materialize_dynamic_playlist(item):
    """Persist an in-memory generated mix when its detail is requested.

    Detail access is a durable use signal. Persisting here keeps the exact mix
    addressable for future history/library reads while list-only mixes remain
    cheap and disposable.
    """
    if item is None or not getattr(item, '_is_dynamic', False):
        return item
    with transaction.atomic():
        stored, created = RecommendedPlaylist.objects.get_or_create(
            unique_id=item.unique_id,
            defaults={
                'user': None,
                'title': item.title,
                'title_en': item.title_en,
                'description': item.description,
                'description_en': item.description_en,
                'playlist_type': item.playlist_type,
                'song_order': list(item.song_order or []),
                'relevance_score': item.relevance_score,
                'match_percentage': item.match_percentage,
                'expires_at': item.expires_at,
            },
        )
        songs = list(getattr(item, '_detail_songs', []) or getattr(item, '_card_songs', []))
        if created and songs:
            RecommendedPlaylist.songs.through.objects.bulk_create(
                [
                    RecommendedPlaylist.songs.through(
                        recommendedplaylist_id=stored.pk, song_id=song.pk
                    )
                    for song in songs
                ],
                ignore_conflicts=True,
                batch_size=500,
            )
        stored._detail_songs = songs
        stored._card_songs = songs
        return stored



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


def _song_recommendation_candidate_ids(request, pool_limit=240):
    """Build a high-quality ranked candidate pool and cache only the ranking.

    Per-request freshness is applied after this function, so the expensive taste
    analysis remains fast while the visible list does not repeat.
    """
    user = request.user
    require_preview = not user.is_authenticated
    if require_preview:
        return 'daily_trending', _guest_daily_song_ids(pool_limit)

    catalog_version = cache_version(CATALOG_VERSION_KEY)
    affinity_version = user_affinity_version(user.pk)
    key = stable_cache_key(
        'home-song-candidate-pool', user.pk, catalog_version,
        affinity_version, pool_limit, 'v7',
    )
    cached = cache_get(key)
    if cached:
        return cached.get('type', 'personalized'), cached.get('ids', [])

    base = _home_song_queryset()
    liked = set(SongLike.objects.filter(user=user).values_list('song_id', flat=True))
    played = set(
        Song.objects.filter(play_counts__user=user)
        .order_by('-play_counts__created_at')
        .values_list('id', flat=True)[:1500]
    )
    playlist = set(
        UserPlaylist.songs.through.objects.filter(userplaylist__user=user)
        .values_list('song_id', flat=True)[:1500]
    )
    interacted_ids = liked | played | playlist
    recommendation_type = 'personalized'

    ranked_ids = []
    if interacted_ids:
        interacted = Song.objects.filter(id__in=interacted_ids)
        genre_ids = list(
            interacted.exclude(genres__id=None)
            .values('genres__id').annotate(n=Count('id')).order_by('-n')
            .values_list('genres__id', flat=True)[:5]
        )
        mood_ids = list(
            interacted.exclude(moods__id=None)
            .values('moods__id').annotate(n=Count('id')).order_by('-n')
            .values_list('moods__id', flat=True)[:4]
        )
        artist_ids = list(
            interacted.values('artist_id').annotate(n=Count('id')).order_by('-n')
            .values_list('artist_id', flat=True)[:6]
        )
        if genre_ids or mood_ids or artist_ids:
            ranked_ids = list(
                base.exclude(id__in=interacted_ids).filter(
                    Q(genres__id__in=genre_ids)
                    | Q(moods__id__in=mood_ids)
                    | Q(artist_id__in=artist_ids)
                ).distinct().order_by('-plays', '-release_date', '-created_at')
                .values_list('id', flat=True)[:pool_limit]
            )
        else:
            recommendation_type = 'trending'
    else:
        recommendation_type = 'trending'

    # Fill from strong catalog candidates without weakening the personalized top.
    if len(ranked_ids) < pool_limit:
        seen = set(ranked_ids) | interacted_ids
        ranked_ids.extend(
            base.exclude(id__in=seen)
            .order_by('-plays', '-release_date', '-created_at')
            .values_list('id', flat=True)[:pool_limit - len(ranked_ids)]
        )
    # Small catalogs may not have twelve unseen songs; only then reuse the best
    # interacted tracks so the section remains complete rather than disappearing.
    if len(ranked_ids) < pool_limit:
        seen = set(ranked_ids)
        ranked_ids.extend(
            base.exclude(id__in=seen)
            .order_by('-plays', '-release_date', '-created_at')
            .values_list('id', flat=True)[:pool_limit - len(ranked_ids)]
        )

    ranked_ids = list(dict.fromkeys(ranked_ids))
    cache_set(
        key,
        {'type': recommendation_type, 'ids': ranked_ids},
        max(15, int(getattr(settings, 'CACHE_TTL_USER_HOME', 30))),
    )
    return recommendation_type, ranked_ids


def _song_recommendations(request, limit=12, *, remember=True, scope='today-picks'):
    recommendation_type, ranked_ids = _song_recommendation_candidate_ids(
        request, max(120, int(limit) * 12)
    )
    if request.user.is_authenticated:
        exposure_scope = f'{scope}:user:{request.user.pk}'
        # Keep novelty inside the strongest quality band. Redis atomically selects
        # and records exposure so concurrent home requests do not repeat a slice.
        freshness_pool = ranked_ids[:max(int(limit) * 6, 72)]
        selected_ids = fresh_select_ids(
            exposure_scope, freshness_pool, limit=limit,
            recent_window=max(96, int(limit) * 8),
        )
    else:
        exposure_scope = None
        selected_ids = ranked_ids[:limit]

    song_map = _home_song_queryset(not request.user.is_authenticated).filter(
        id__in=selected_ids
    ).in_bulk()
    songs = [song_map[song_id] for song_id in selected_ids if song_id in song_map]
    if request.user.is_authenticated and len(songs) < limit:
        existing_ids = {song.id for song in songs}
        fallback = list(
            _home_song_queryset().exclude(id__in=existing_ids)
            .order_by('-plays', '-release_date', '-created_at')[:limit - len(songs)]
        )
        songs.extend(fallback)
    hydrate_song_metrics(songs, request.user if request.user.is_authenticated else None)

    return recommendation_type, songs, len(ranked_ids) > len(songs)


def _paginated_payload(request, items, page_param, page, size, serializer):
    page_items, has_next = _slice_items(items, page, size)
    return {
        'count': len(page_items),
        'next': _next_url(request, page_param, page, has_next),
        'previous': None,
        'results': serializer(page_items, many=True, context={'request': request}).data,
    }


TRENDING_MIN_SONGS = 6
TRENDING_MAX_SONGS = 12
TRENDING_WINDOWS_DAYS = (7, 14, 30, 60, 90, 180, 365)


def _trending_song_ids(*, require_preview=False):
    """Return genuinely played songs from the smallest useful recent window.

    The ranking begins with seven days. When fewer than six distinct eligible
    songs were played, it progressively widens the period until six are
    available. The final fallback is all recorded history plus the legacy
    aggregate ``Song.plays`` counter; songs with zero real plays are never used.
    Results are globally cacheable because the ranking is not user-specific.
    """
    version = cache_version(CATALOG_VERSION_KEY)
    cache_key = stable_cache_key(
        'home-trending-songs',
        'preview' if require_preview else 'full',
        version,
        'v1',
    )
    cached = cache_get(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get('ids'), list):
        return cached

    now = timezone.now()
    through = Song.play_counts.through
    link_filter = Q(song__status=Song.STATUS_PUBLISHED)
    if require_preview:
        link_filter &= Q(song__preview_audio_url__isnull=False)
        link_filter &= ~Q(song__preview_audio_url='')

    annotations = {
        'recorded_plays_all': Count('playcount_id'),
        'last_play_all': Max('playcount__created_at'),
    }
    for days in TRENDING_WINDOWS_DAYS:
        cutoff = now - timedelta(days=days)
        annotations[f'recorded_plays_{days}'] = Count(
            'playcount_id',
            filter=Q(playcount__created_at__gte=cutoff),
        )
        annotations[f'last_play_{days}'] = Max(
            'playcount__created_at',
            filter=Q(playcount__created_at__gte=cutoff),
        )

    rows = list(
        through.objects.filter(link_filter)
        .values('song_id')
        .annotate(**annotations)
    )

    selected_window = None
    candidates = []
    for days in TRENDING_WINDOWS_DAYS:
        score_field = f'recorded_plays_{days}'
        period_rows = [row for row in rows if int(row.get(score_field) or 0) > 0]
        if len(period_rows) >= TRENDING_MIN_SONGS:
            selected_window = days
            candidates = period_rows
            break

    if selected_window is not None:
        score_field = f'recorded_plays_{selected_window}'
        last_field = f'last_play_{selected_window}'
        candidates.sort(
            key=lambda row: (
                int(row.get(score_field) or 0),
                row.get(last_field) or row.get('last_play_all') or now - timedelta(days=36500),
                int(row.get('recorded_plays_all') or 0),
                int(row['song_id']),
            ),
            reverse=True,
        )
        result = {
            'ids': [int(row['song_id']) for row in candidates[:TRENDING_MAX_SONGS]],
            'window_days': selected_window,
            'is_all_time': False,
        }
    else:
        # Some older installations only populated Song.plays. Include that real
        # all-time counter only after every timestamped window has been exhausted.
        row_by_song = {int(row['song_id']): row for row in rows}
        song_filter = Q(status=Song.STATUS_PUBLISHED)
        if require_preview:
            song_filter &= Q(preview_audio_url__isnull=False)
            song_filter &= ~Q(preview_audio_url='')
        legacy_rows = Song.objects.filter(song_filter).filter(
            Q(plays__gt=0) | Q(id__in=row_by_song.keys())
        ).values('id', 'plays')

        all_time = []
        for song_row in legacy_rows:
            song_id = int(song_row['id'])
            event_row = row_by_song.get(song_id, {})
            recorded = int(event_row.get('recorded_plays_all') or 0)
            legacy = int(song_row.get('plays') or 0)
            total = recorded + legacy
            if total <= 0:
                continue
            all_time.append({
                'song_id': song_id,
                'score': total,
                'recorded': recorded,
                'last_play': event_row.get('last_play_all'),
            })

        all_time.sort(
            key=lambda row: (
                row['score'],
                row['last_play'] or now - timedelta(days=36500),
                row['recorded'],
                row['song_id'],
            ),
            reverse=True,
        )
        result = {
            'ids': [row['song_id'] for row in all_time[:TRENDING_MAX_SONGS]],
            'window_days': None,
            'is_all_time': True,
        }

    # Short TTL keeps the section responsive to new plays without executing the
    # aggregate query on every home request. Redis shares it across workers.
    cache_set(cache_key, result, 180)
    return result


def _trending_songs(request):
    ranking = _trending_song_ids(require_preview=not request.user.is_authenticated)
    ids = ranking['ids']
    song_map = _home_song_queryset(not request.user.is_authenticated).filter(
        id__in=ids
    ).in_bulk()
    songs = [song_map[song_id] for song_id in ids if song_id in song_map]
    hydrate_song_metrics(
        songs,
        request.user if request.user.is_authenticated else None,
    )
    return ranking, songs


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
        cache_key = stable_cache_key('home-summary', get_request_language(request), audience, version, pages, 'v14')
        cached, claimed = cache_get_or_claim(cache_key) if not user.is_authenticated else (None, False)
        if cached is not None:
            return Response(cached)

        rec_size = 12
        if user.is_authenticated:
            # Authenticated users always receive a complete, fresh twelve-item
            # Today's Picks set. Ranking is cached; exposure rotation is Redis-only.
            rec_type, rec_page, rec_next = _song_recommendations(
                request, rec_size, remember=True, scope='home-today-picks'
            )
            rec_songs = rec_page
        else:
            rec_type, rec_songs, _ = _song_recommendations(
                request, 48, remember=False, scope='guest-home-today-picks'
            )
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

        if user.is_authenticated:
            _ensure_personal_recommendations(user, target=18)
        playlists = _playlist_recommendation_items(user, 80)
        playlist_page, playlist_next = _slice_items(playlists, pages['playlists'], 6)
        _attach_recommended_metrics(playlist_page, user)
        _remember_playlist_results(user, playlist_page)

        discovery_base = _home_song_queryset(not user.is_authenticated)
        excluded = {song.id for song in latest[:30]} | {song.id for song in rec_songs}
        discovery_pool = list(discovery_base.exclude(id__in=excluded).order_by('-created_at')[:120])
        discoveries = _rotate_sample(discovery_pool, 60, f'{audience}:{timezone.now():%Y-%m-%d-%H}')
        discovery_page, discovery_next = _slice_items(discoveries, pages['discoveries'], 6)
        hydrate_song_metrics(discovery_page, user if user.is_authenticated else None)

        trending_ranking, trending_songs = _trending_songs(request)

        payload = {
            'sections': 7,
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
            'trending': {
                'count': len(trending_songs),
                'window_days': trending_ranking['window_days'],
                'is_all_time': trending_ranking['is_all_time'],
                'minimum_met': len(trending_songs) >= TRENDING_MIN_SONGS,
                'results': SongSummarySerializer(
                    trending_songs, many=True, context={'request': request}
                ).data,
            },
        }
        if claimed and not user.is_authenticated:
            cache_set(cache_key, payload, getattr(settings, 'CACHE_TTL_HOME', 90))
        return Response(payload)



class UserRecommendationView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        _, size = _page_values(request, 20 if not request.user.is_authenticated else 12, 50)
        if request.user.is_authenticated:
            size = max(12, size)
        recommendation_type, songs, _ = _song_recommendations(
            request, size, remember=True, scope='today-picks-endpoint'
        )
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
        return _ensure_personal_recommendations(user, target=18)

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
        _remember_playlist_results(request.user, items)
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
            instance = _materialize_dynamic_playlist(instance)
        if instance is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        # A detail GET is durable use: keep the exact generated row permanently.
        RecommendedPlaylist.objects.filter(pk=instance.pk).update(
            views=F('views') + 1, expires_at=None
        )
        instance.views = int(getattr(instance, 'views', 0) or 0) + 1
        instance.expires_at = None
        if request.user.is_authenticated:
            instance.viewed_by.add(request.user)
        mark_generated_playlist_usage([instance])

        songs = list(getattr(instance, '_detail_songs', []))
        if not songs:
            songs = list(_home_song_queryset(not request.user.is_authenticated).filter(
                recommended_playlists=instance
            ).order_by('-release_date', '-created_at'))
            instance._detail_songs = songs
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
                    'is_liked': serializers.BooleanField(),
                }
            )
        }
    )
    def post(self, request, unique_id):
        from .models import RecommendedPlaylist

        try:
            playlist = RecommendedPlaylist.objects.get(unique_id=unique_id)
            if playlist.expires_at is not None:
                RecommendedPlaylist.objects.filter(pk=playlist.pk).update(expires_at=None)
                playlist.expires_at = None
            mark_generated_playlist_usage([playlist])
        except RecommendedPlaylist.DoesNotExist:
            return Response(
                {'error': 'Playlist not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        user = request.user
        requested_liked = request.data.get('liked')
        if not isinstance(requested_liked, bool):
            requested_liked = None

        # Check if already liked
        is_liked = playlist.liked_by.filter(id=user.id).exists()

        # New clients send the desired target state. This makes retries and
        # rapid taps idempotent instead of toggling twice. Legacy clients that
        # omit `liked` retain the existing toggle contract.
        if requested_liked is True and is_liked:
            return Response({
                'status': 'liked',
                'is_liked': True,
                'likes_count': playlist.liked_by.count(),
            })
        if requested_liked is False and not is_liked:
            return Response({
                'status': 'unliked',
                'is_liked': False,
                'likes_count': playlist.liked_by.count(),
            })

        should_like = (not is_liked) if requested_liked is None else requested_liked

        if not should_like:
            # Unlike: remove PlaylistLike if present, otherwise fall back to M2M
            from .models import PlaylistLike
            pl_like_qs = PlaylistLike.objects.filter(user=user, playlist_id=playlist.id)
            if pl_like_qs.exists():
                pl_like_qs.delete()
                return Response({
                    'status': 'unliked',
                    'is_liked': False,
                    'likes_count': PlaylistLike.objects.filter(playlist=playlist).count(),
                })
            # fallback for RecommendedPlaylist M2M
            playlist.liked_by.remove(user)
            return Response({
                'status': 'unliked',
                'is_liked': False,
                'likes_count': playlist.liked_by.count(),
            })
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
                    'is_liked': True,
                    'likes_count': frozen_playlist.liked_by.count(),
                    'new_unique_id': new_id,
                    'is_frozen': True
                })
            else:
                # Direct like for already persistent or other types
                playlist.liked_by.add(user)
                return Response({
                    'status': 'liked',
                    'is_liked': True,
                    'likes_count': playlist.liked_by.count(),
                })


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
            if playlist.expires_at is not None:
                RecommendedPlaylist.objects.filter(pk=playlist.pk).update(expires_at=None)
                playlist.expires_at = None
            mark_generated_playlist_usage([playlist])
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


_SEARCH_FILTER_MARKS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED\u0640\u200C\s]+")
_SEARCH_CHAR_TRANSLATION = str.maketrans({
    'ي': 'ی',
    'ى': 'ی',
    'ك': 'ک',
    'ة': 'ه',
    'ۀ': 'ه',
})
_SEARCH_DB_REPLACEMENTS = (
    ('ي', 'ی'),
    ('ى', 'ی'),
    ('ك', 'ک'),
    ('ة', 'ه'),
    ('ۀ', 'ه'),
    (' ', ''),
    ('\u200c', ''),
    ('\u0640', ''),
    ('\t', ''),
    ('\n', ''),
    ('\r', ''),
) + tuple((chr(codepoint), '') for codepoint in (
    *range(0x064B, 0x0660),
    0x0670,
    *range(0x06D6, 0x06EE),
))


def _normalize_directory_search(value):
    """Normalize Persian/Arabic names and identifiers for directory search."""
    normalized = str(value or '').translate(_SEARCH_CHAR_TRANSLATION).casefold()
    return _SEARCH_FILTER_MARKS_RE.sub('', normalized)


def _normalized_directory_expression(*field_names):
    """Build one typed normalized SQL expression shared by artists and users."""
    pieces = []
    separator = Value(' ', output_field=TextField())
    empty = Value('', output_field=TextField())
    for index, field_name in enumerate(field_names):
        if index:
            pieces.append(separator)
        pieces.append(
            Coalesce(
                Cast(F(field_name), TextField()),
                empty,
                output_field=TextField(),
            )
        )
    expression = Concat(*pieces, output_field=TextField())
    for source, target in _SEARCH_DB_REPLACEMENTS:
        expression = Replace(
            expression,
            Value(source, output_field=TextField()),
            Value(target, output_field=TextField()),
            output_field=TextField(),
        )
    return expression


@extend_schema(tags=['Search Page Endpoints اندپوینت های صفحه جستجو'])
class SearchView(APIView):
    permission_classes = [AllowAny]
    TYPES = ('song', 'artist', 'album', 'playlist', 'user')

    def get(self, request):
        query = request.query_params.get('q', '').strip()
        search_type = (request.query_params.get('type') or '').strip().lower() or None
        moods = sorted(value for value in request.query_params.getlist('moods') if value)
        page, page_size = _page_values(request, 20, 100)

        if search_type and search_type not in self.TYPES:
            return Response(
                {'error': 'Invalid type. Must be song, artist, album, playlist, or user.'},
                status=400,
            )

        key = stable_cache_key(
            'search-ids-v12',
            query.casefold(),
            search_type or 'mixed',
            moods,
            page,
            page_size,
            cache_version(CATALOG_VERSION_KEY),
            cache_version(USER_DIRECTORY_VERSION_KEY),
        )
        cached, _ = cache_get_or_claim(key)

        if cached is None:
            offset = (page - 1) * page_size

            if search_type:
                queryset = self._queryset(search_type, query, moods, request)
                ids = list(
                    queryset.values_list('id', flat=True)[
                        offset:offset + page_size + 1
                    ]
                )
                cached = {
                    'refs': [(search_type, pk) for pk in ids[:page_size]],
                    'has_next': len(ids) > page_size,
                }
            else:
                # Build one deterministic, globally paginated mixed stream.
                # The old per-type offset skipped valid results whenever a small
                # page was truncated before all categories were emitted.
                window_end = offset + page_size + 1
                groups = []
                for kind in self.TYPES:
                    ids = list(
                        self._queryset(kind, query, moods, request)
                        .values_list('id', flat=True)[:window_end]
                    )
                    groups.append([(kind, pk) for pk in ids])

                mixed_refs = []
                max_group_size = max((len(group) for group in groups), default=0)
                for index in range(max_group_size):
                    for group in groups:
                        if index < len(group):
                            mixed_refs.append(group[index])
                            if len(mixed_refs) >= window_end:
                                break
                    if len(mixed_refs) >= window_end:
                        break

                cached = {
                    'refs': mixed_refs[offset:offset + page_size],
                    'has_next': len(mixed_refs) > offset + page_size,
                }

            cache_set(key, cached, getattr(settings, 'CACHE_TTL_SEARCH', 45))

        results = self._hydrate(cached.get('refs') or [], request)
        serialized_results = SearchResultSerializer(
            results,
            many=True,
            context={'request': request},
        ).data
        counts = {kind: 0 for kind in self.TYPES}
        for item in serialized_results:
            item_type = item.get('type')
            if item_type in counts:
                counts[item_type] += 1

        return Response({
            'results': serialized_results,
            'page': page,
            'page_size': page_size,
            'has_next': bool(cached.get('has_next', False)),
            'query': query,
            'moods': moods,
            'type': search_type or 'mixed',
            'counts': counts,
            'total_results': len(serialized_results),
            'is_empty': not serialized_results,
        })

    def _queryset(self, kind, query, moods, request):
        return {'song':self._search_songs,'artist':self._search_artists,'album':self._search_albums,
                'playlist':self._search_playlists,'user':self._search_users}[kind](query, moods, request)

    def _search_songs(self,q,moods,request):
        qs=Song.objects.filter(status=Song.STATUS_PUBLISHED)
        if q:
            clean=q.replace(' ','').replace('\u200c','')
            qs=qs.annotate(
                t_clean=Replace(
                    Replace(
                        Cast('title', TextField()),
                        Value(' '),
                        Value(''),
                        output_field=TextField(),
                    ),
                    Value('\u200c'),
                    Value(''),
                    output_field=TextField(),
                ),
                a_clean=Replace(
                    Replace(
                        Cast('artist__name', TextField()),
                        Value(' '),
                        Value(''),
                        output_field=TextField(),
                    ),
                    Value('\u200c'),
                    Value(''),
                    output_field=TextField(),
                ),
            )
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
        if q:
            normalized = _normalize_directory_search(q)
            qs = qs.annotate(
                directory_search=_normalized_directory_expression(
                    'name', 'name_en', 'artistic_name', 'artistic_name_en', 'unique_id'
                )
            ).filter(
                Q(name__icontains=q)|Q(name_en__icontains=q)|
                Q(artistic_name__icontains=q)|Q(artistic_name_en__icontains=q)|
                Q(bio__icontains=q)|Q(bio_en__icontains=q)|Q(unique_id__icontains=q)|
                (Q(directory_search__icontains=normalized) if normalized else Q())
            )
        return qs.order_by('-verified','-created_at')
    def _search_albums(self,q,moods,request):
        qs=Album.objects.filter(songs__status=Song.STATUS_PUBLISHED).exclude(Q(title__iexact='single')|Q(title='سینگل')).distinct()
        if q: qs=qs.filter(Q(title__icontains=q)|Q(title_en__icontains=q)|Q(description__icontains=q)|Q(description_en__icontains=q)|Q(artist__name__icontains=q)|Q(artist__name_en__icontains=q))
        return qs.order_by('-release_date','-created_at')
    def _search_playlists(self,q,moods,request):
        qs=Playlist.objects.all()
        if q: qs=qs.filter(Q(title__icontains=q)|Q(title_en__icontains=q)|Q(description__icontains=q)|Q(description_en__icontains=q))
        if moods: qs=qs.filter(Q(moods__id__in=moods) if all(x.isdigit() for x in moods) else Q(moods__slug__in=moods))
        return qs.distinct().order_by('-created_at')
    def _search_users(self,q,moods,request):
        qs=User.objects.filter(
            is_active=True,
            is_banned=False,
            roles__contains=User.ROLE_AUDIENCE,
        ).exclude(Q(unique_id__isnull=True)|Q(unique_id=''))
        if request.user.is_authenticated:
            qs=qs.exclude(pk=request.user.pk)
        if q:
            normalized = _normalize_directory_search(q)
            qs = qs.annotate(
                directory_search=_normalized_directory_expression(
                    'first_name', 'last_name', 'unique_id'
                ),
                search_rank=Case(
                    When(unique_id__iexact=q, then=Value(0)),
                    When(first_name__iexact=q, then=Value(1)),
                    When(last_name__iexact=q, then=Value(1)),
                    default=Value(2),
                    output_field=IntegerField(),
                ),
            ).filter(
                Q(unique_id__icontains=q)|Q(first_name__icontains=q)|Q(last_name__icontains=q)|
                (Q(directory_search__icontains=normalized) if normalized else Q())
            )
            return qs.order_by('search_rank', '-date_joined')
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
            Prefetch('playlists__songs', queryset=Song.objects.select_related('album')),
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
                            'today': serializers.DecimalField(max_digits=12, decimal_places=8),
                            'last_7_days': serializers.DecimalField(max_digits=12, decimal_places=8),
                            'last_30_days': serializers.DecimalField(max_digits=12, decimal_places=8),
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
                total_income=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=12, decimal_places=8))),
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
        top_songs_qs = list(Song.objects.filter(artist=artist).select_related(
            'artist', 'album', 'uploader'
        ).prefetch_related('featured_artists', 'genres', 'sub_genres', 'moods', 'tags').annotate(
            total_plays_calc=ExpressionWrapper(F('plays') + Count('play_counts'), output_field=BigIntegerField())
        ).order_by('-total_plays_calc')[:6])
        hydrate_song_metrics(top_songs_qs, request.user)
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
    """Real-time compatible analytics for the authenticated artist."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        artist = getattr(user, 'artist_profile', None)
        if not artist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        period = request.query_params.get('period', '30d').lower()
        chart_type = request.query_params.get('chart', '').lower()
        allowed_periods = {'today', '7d', '30d', '365d', 'all'}
        if period not in allowed_periods:
            return Response({"error": "Invalid period. Use today, 7d, 30d, 365d, or all."}, status=status.HTTP_400_BAD_REQUEST)
        if chart_type and chart_type not in {'hourly', 'daily', 'monthly'}:
            return Response({"error": "Invalid chart type. Use hourly, daily, or monthly."}, status=status.HTTP_400_BAD_REQUEST)

        now = timezone.now()
        today_date = timezone.localdate(now)
        today = _finance_day_start(today_date)
        current_month = _finance_month_start(today_date)
        twelve_month_start = _finance_shift_month(current_month, -11)
        windows = {
            'today': (today, today - timedelta(days=1), today),
            '7d': (_finance_day_start(today_date - timedelta(days=6)), _finance_day_start(today_date - timedelta(days=13)), _finance_day_start(today_date - timedelta(days=6))),
            '30d': (_finance_day_start(today_date - timedelta(days=29)), _finance_day_start(today_date - timedelta(days=59)), _finance_day_start(today_date - timedelta(days=29))),
            '365d': (_finance_day_start(twelve_month_start), _finance_day_start(_finance_shift_month(twelve_month_start, -12)), _finance_day_start(twelve_month_start)),
            'all': (None, None, None),
        }
        start, previous_start, previous_end = windows[period]
        chart_type = chart_type or ('hourly' if period == 'today' else 'monthly' if period in {'365d', 'all'} else 'daily')

        def period_qs(model, field='created_at'):
            qs = model
            if start:
                qs = qs.filter(**{f'{field}__gte': start})
            return qs

        active_songs = Song.objects.filter(artist=artist).exclude(status=Song.STATUS_DELETED)
        current_plays = period_qs(PlayCount.objects.filter(songs__in=active_songs)).distinct()
        previous_plays = PlayCount.objects.none()
        if previous_start and previous_end:
            previous_plays = PlayCount.objects.filter(
                songs__in=active_songs,
                created_at__gte=previous_start,
                created_at__lt=previous_end,
            ).distinct()

        current_likes = period_qs(SongLike.objects.filter(song__in=active_songs))
        current_followers = period_qs(Follow.objects.filter(followed_artist=artist))
        previous_likes = SongLike.objects.none()
        previous_followers = Follow.objects.none()
        if previous_start and previous_end:
            previous_likes = SongLike.objects.filter(song__in=active_songs, created_at__gte=previous_start, created_at__lt=previous_end)
            previous_followers = Follow.objects.filter(followed_artist=artist, created_at__gte=previous_start, created_at__lt=previous_end)

        current_income = current_plays.aggregate(total=Coalesce(
            Sum('pay'), Value(0, output_field=DecimalField(max_digits=20, decimal_places=8))
        ))['total']
        previous_income = previous_plays.aggregate(total=Coalesce(
            Sum('pay'), Value(0, output_field=DecimalField(max_digits=20, decimal_places=8))
        ))['total']
        current_play_count = current_plays.count()
        previous_play_count = previous_plays.count()
        if period == 'all':
            current_play_count += active_songs.aggregate(total=Coalesce(Sum('plays'), Value(0, output_field=BigIntegerField())))['total'] or 0

        def change(current, previous):
            if previous in (None, 0):
                return None
            return round(((float(current) - float(previous)) / float(previous)) * 100, 1)

        summary = {
            'total_plays': int(current_play_count),
            'total_likes': current_likes.count(),
            'total_income': current_income,
            'total_followers': Follow.objects.filter(followed_artist=artist).count(),
            'new_followers': current_followers.count() if period != 'all' else Follow.objects.filter(followed_artist=artist).count(),
            'unique_listeners': current_plays.values('user_id').distinct().count(),
            'monthly_listeners': ArtistMonthlyListener.objects.filter(artist=artist, updated_at__gte=now - timedelta(days=28)).count(),
            'period': period,
            'growth': {
                'plays': change(current_play_count, previous_play_count),
                'likes': change(current_likes.count(), previous_likes.count()),
                'income': change(current_income, previous_income),
                'followers': change(current_followers.count(), previous_followers.count()),
            },
        }

        chart_qs = current_plays
        bucket = TruncHour('created_at') if chart_type == 'hourly' else TruncMonth('created_at') if chart_type == 'monthly' else TruncDate('created_at')
        chart_rows = chart_qs.annotate(bucket=bucket).values('bucket').annotate(count=Count('id', distinct=True)).order_by('bucket')

        def chart_bucket_key(value):
            if chart_type == 'hourly':
                if timezone.is_aware(value):
                    value = timezone.localtime(value)
                return value.replace(minute=0, second=0, microsecond=0)
            return _finance_bucket_key(value, 'monthly' if chart_type == 'monthly' else 'daily')

        chart_counts = {chart_bucket_key(row['bucket']): int(row['count']) for row in chart_rows}
        if chart_type == 'hourly':
            chart_start = today
            chart_end = today + timedelta(hours=23)
            chart_buckets = (chart_start + timedelta(hours=hour) for hour in range(24))
        elif chart_type == 'monthly':
            chart_end = current_month
            if period == 'all':
                chart_start = min(chart_counts, default=chart_end)
            elif period == '365d':
                chart_start = twelve_month_start
            else:
                chart_start = _finance_month_start(timezone.localdate(start)) if start else chart_end
            chart_buckets = _finance_bucket_range('monthly', chart_start, chart_end)
        else:
            chart_end = today_date
            if period == 'all':
                chart_start = min(chart_counts, default=chart_end)
            else:
                chart_start = timezone.localdate(start) if start else chart_end
            chart_buckets = _finance_bucket_range('daily', chart_start, chart_end)

        chart = [{'time': item.isoformat(), 'count': chart_counts.get(item, 0)} for item in chart_buckets]

        base_count = current_plays.count()
        def distribution(field, label):
            rows = current_plays.values(field).annotate(count=Count('id', distinct=True)).order_by('-count')[:20]
            return [{
                label: row[field] or 'Unknown',
                'count': row['count'],
                'percentage': round((row['count'] / base_count * 100), 2) if base_count else 0,
            } for row in rows]

        plan_rows = current_plays.values('user__plan').annotate(
            count=Count('id', distinct=True),
            income=Coalesce(Sum('pay'), Value(0, output_field=DecimalField(max_digits=20, decimal_places=8))),
        ).order_by('user__plan')
        plan_distribution = [{
            'plan': row['user__plan'] or User.PLAN_FREE,
            'count': row['count'],
            'income': row['income'],
            'percentage': round((row['count'] / base_count * 100), 2) if base_count else 0,
        } for row in plan_rows]

        period_filter = Q(play_counts__created_at__gte=start) if start else Q()
        top_qs = active_songs.annotate(
            period_plays=Count('play_counts', filter=period_filter, distinct=True),
            likes_total=Count('liked_by', distinct=True),
        )
        if period == 'all':
            top_qs = top_qs.annotate(ranked_plays=ExpressionWrapper(F('plays') + F('period_plays'), output_field=BigIntegerField()))
        else:
            top_qs = top_qs.annotate(ranked_plays=F('period_plays'))
        top_qs = top_qs.order_by('-ranked_plays', '-likes_total', '-created_at')[:10]
        top_songs = [{
            'id': song.id,
            'title': song.title,
            'title_en': song.title_en,
            'cover_image': generate_signed_r2_url(song.cover_image) if song.cover_image else '',
            'plays': int(song.ranked_plays or 0),
            'likes': int(song.likes_total or 0),
            'stream_share': round((int(song.ranked_plays or 0) / current_play_count * 100), 2) if current_play_count else 0,
        } for song in top_qs]

        return Response({
            'summary': summary,
            'chart': {'type': chart_type, 'data': chart},
            'city_distribution': distribution('city', 'city'),
            'country_distribution': distribution('country', 'country'),
            'plan_distribution': plan_distribution,
            'top_songs': top_songs,
        })


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class DepositRequestView(APIView):
    permission_classes = [IsAuthenticated]

    def get_artist(self, user):
        if User.ROLE_ARTIST not in user.roles:
            return None
        return getattr(user, 'artist_profile', None)

    def get(self, request, pk=None):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)
        requests = DepositRequest.objects.filter(artist=artist)
        if pk is not None:
            item = get_object_or_404(requests, pk=pk)
            return Response(DepositRequestSerializer(item).data)
        return Response(DepositRequestSerializer(requests, many=True).data)

    def post(self, request, pk=None):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            Artist.objects.select_for_update().only('id').get(pk=artist.pk)
            active = DepositRequest.objects.select_for_update().filter(
                artist=artist,
                status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED],
            )
            if active.exists():
                return Response({"error": "You already have an active payout request."}, status=status.HTTP_400_BAD_REQUEST)

            plays = PlayCount.objects.filter(songs__artist=artist).distinct()
            total_credit = plays.aggregate(total=Coalesce(
                Sum('pay'), _finance_zero()
            ))['total']
            reserved = DepositRequest.objects.filter(
                artist=artist,
                status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_DONE],
            ).aggregate(total=Coalesce(
                Sum('amount'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=2))
            ))['total']
            available = max(Decimal('0'), total_credit - reserved).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
            if available < Decimal('0.01'):
                return Response({"error": "No available balance to request a payout."}, status=status.HTTP_400_BAD_REQUEST)

            total_plays = plays.count()
            free_plays = plays.filter(user__plan=User.PLAN_FREE).count()
            premium_plays = plays.filter(user__plan=User.PLAN_PREMIUM).count()
            song_totals = _finance_song_totals(artist)
            existing_requests = DepositRequest.objects.filter(
                artist=artist,
                status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_DONE],
            ).order_by('submission_date', 'pk')
            already_reserved, _, _ = _finance_artist_song_allocations(
                artist,
                song_totals=song_totals,
                requests=existing_requests,
            )
            song_allocations = _finance_allocate_across_songs(song_totals, already_reserved, available)
            if abs(sum(song_allocations.values(), Decimal('0')) - _finance_decimal(available)) > FINANCE_QUANTUM:
                return Response({"error": "Could not allocate the payout across songs."}, status=status.HTTP_409_CONFLICT)

            summary = {
                'total_plays': total_plays,
                'free_plays': free_plays,
                'premium_plays': premium_plays,
                'free_percentage': round((free_plays / total_plays * 100), 1) if total_plays else 0,
                'premium_percentage': round((premium_plays / total_plays * 100), 1) if total_plays else 0,
                'allocation_version': 1,
                'song_allocations': [
                    {'song_id': song_id, 'amount': _finance_string(amount)}
                    for song_id, amount in sorted(song_allocations.items())
                ],
            }
            item = DepositRequest.objects.create(artist=artist, amount=available, summary=summary)
        return Response(DepositRequestSerializer(item).data, status=status.HTTP_201_CREATED)

    def delete(self, request, pk=None):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)
        if pk is None:
            return Response({"error": "Payout request id is required."}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            item = get_object_or_404(DepositRequest.objects.select_for_update(), pk=pk, artist=artist)
            if item.status != DepositRequest.STATUS_PENDING:
                return Response({"error": "Only pending payout requests can be cancelled."}, status=status.HTTP_400_BAD_REQUEST)
            item.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistWalletView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        artist = getattr(user, 'artist_profile', None)
        if not artist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        plays = PlayCount.objects.filter(songs__artist=artist).distinct()
        total_credit = plays.aggregate(total=Coalesce(
            Sum('pay'), _finance_zero()
        ))['total']
        requests = DepositRequest.objects.filter(artist=artist)
        reserved = requests.filter(status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_DONE]).aggregate(total=Coalesce(
            Sum('amount'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=2))
        ))['total']
        withdrawn = requests.filter(status=DepositRequest.STATUS_DONE).aggregate(total=Coalesce(
            Sum('amount'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=2))
        ))['total']
        pending_amount = requests.filter(status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED]).aggregate(total=Coalesce(
            Sum('amount'), Value(0, output_field=DecimalField(max_digits=15, decimal_places=2))
        ))['total']
        available = max(Decimal('0'), total_credit - reserved)
        withdrawable = available.quantize(Decimal('0.01'), rounding=ROUND_DOWN)

        return Response({
            'total_credit': _finance_string(total_credit),
            'requested_credit': _finance_string(reserved),
            'available_credit': _finance_string(available),
            'withdrawable_credit': format(withdrawable, 'f'),
            'withdrawn_credit': _finance_string(withdrawn),
            'pending_credit': _finance_string(pending_amount),
            'paid_plays': plays.filter(pay__gt=0).count(),
            'zero_value_plays': plays.filter(pay=0).count(),
            'has_active_request': requests.filter(status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED]).exists(),
            'deposit_requests': {
                'total_submissions': requests.count(),
                'pending': requests.filter(status=DepositRequest.STATUS_PENDING).count(),
                'approved': requests.filter(status=DepositRequest.STATUS_APPROVED).count(),
                'rejected': requests.filter(status=DepositRequest.STATUS_REJECTED).count(),
                'done': requests.filter(status=DepositRequest.STATUS_DONE).count(),
            },
        })


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistFinanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        artist = getattr(user, 'artist_profile', None)
        if not artist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        period = request.query_params.get('period', '30d').lower()
        today = timezone.localdate()
        tomorrow = today + timedelta(days=1)

        if period in ('all', 'lifetime'):
            start = previous_start = previous_end = None
            end = _finance_day_start(tomorrow)
            group = 'monthly'
            chart_start = None
            chart_end = _finance_month_start(today)
            period = 'all'
        elif period in ('7d', 'week'):
            chart_start = today - timedelta(days=6)
            chart_end = today
            start = _finance_day_start(chart_start)
            end = _finance_day_start(tomorrow)
            previous_start = _finance_day_start(chart_start - timedelta(days=7))
            previous_end = start
            group = 'daily'
            period = '7d'
        elif period in ('30d', 'month', 'daily'):
            chart_start = today - timedelta(days=29)
            chart_end = today
            start = _finance_day_start(chart_start)
            end = _finance_day_start(tomorrow)
            previous_start = _finance_day_start(chart_start - timedelta(days=30))
            previous_end = start
            group = 'daily'
            period = '30d'
        elif period in ('monthly', 'year', '365d'):
            chart_end = _finance_month_start(today)
            chart_start = _finance_shift_month(chart_end, -11)
            start = _finance_day_start(chart_start)
            end = _finance_day_start(_finance_shift_month(chart_end, 1))
            previous_start = _finance_day_start(_finance_shift_month(chart_start, -12))
            previous_end = start
            group = 'monthly'
            period = 'monthly'
        elif period == 'weekly':
            chart_end = today - timedelta(days=today.weekday())
            chart_start = chart_end - timedelta(weeks=11)
            start = _finance_day_start(chart_start)
            end = _finance_day_start(chart_end + timedelta(weeks=1))
            previous_start = _finance_day_start(chart_start - timedelta(weeks=12))
            previous_end = start
            group = 'weekly'
        elif period == 'today':
            chart_start = chart_end = today
            start = _finance_day_start(today)
            end = _finance_day_start(tomorrow)
            previous_start, previous_end = start - timedelta(days=1), start
            group = 'daily'
        else:
            return Response({"error": "Invalid period."}, status=status.HTTP_400_BAD_REQUEST)

        current = PlayCount.objects.filter(songs__artist=artist).distinct()
        if start:
            current = current.filter(created_at__gte=start)
        if end:
            current = current.filter(created_at__lt=end)

        previous = PlayCount.objects.none()
        if previous_start and previous_end:
            previous = PlayCount.objects.filter(
                songs__artist=artist,
                created_at__gte=previous_start,
                created_at__lt=previous_end,
            ).distinct()

        def amount(qs):
            return qs.aggregate(total=Coalesce(Sum('pay'), _finance_zero()))['total']

        income = amount(current)
        previous_income = amount(previous)
        plays = current.count()
        free = current.filter(user__plan=User.PLAN_FREE)
        premium = current.filter(user__plan=User.PLAN_PREMIUM)
        pricing = PlayConfiguration.objects.order_by('-updated_at', '-pk').first()
        change = None if previous_income in (None, 0) else round(((float(income) - float(previous_income)) / float(previous_income)) * 100, 1)

        summary = {
            'income_change_pct': change,
            'income_amount': _finance_string(income),
            'currency': 'TOMAN',
            'plays_count': plays,
            'paid_plays': current.filter(pay__gt=0).count(),
            'zero_value_plays': current.filter(pay=0).count(),
            'average_revenue_per_play': _finance_string((income / plays) if plays else 0),
            'free_income': _finance_string(amount(free)),
            'premium_income': _finance_string(amount(premium)),
            'free_plays': free.count(),
            'premium_plays': premium.count(),
            'current_free_play_rate': _finance_string(pricing.free_play_worth if pricing else 0),
            'current_premium_play_rate': _finance_string(pricing.premium_play_worth if pricing else 0),
            'period': period,
        }

        trunc = TruncDate('created_at') if group == 'daily' else TruncWeek('created_at') if group == 'weekly' else TruncMonth('created_at')
        chart_source = PlayCount.objects.filter(pk__in=current.values('pk'))
        rows = list(chart_source.annotate(period_bucket=trunc).values('period_bucket').annotate(
            income=Coalesce(Sum('pay'), _finance_zero()),
            free_income=Coalesce(Sum('pay', filter=Q(user__plan=User.PLAN_FREE)), _finance_zero()),
            premium_income=Coalesce(Sum('pay', filter=Q(user__plan=User.PLAN_PREMIUM)), _finance_zero()),
            plays=Count('id'),
        ).order_by('period_bucket'))

        by_bucket = {_finance_bucket_key(row['period_bucket'], group): row for row in rows}
        if chart_start is None:
            chart_start = min(by_bucket, default=chart_end)

        chart = []
        for bucket in _finance_bucket_range(group, chart_start, chart_end):
            row = by_bucket.get(bucket)
            chart.append({
                'time': bucket.isoformat(),
                'income': _finance_string(row['income'] if row else 0),
                'free_income': _finance_string(row['free_income'] if row else 0),
                'premium_income': _finance_string(row['premium_income'] if row else 0),
                'plays': int(row['plays']) if row else 0,
            })

        return Response({'summary': summary, 'chart': chart})


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistFinanceSongsView(APIView):
    """Paginated song-level earnings, paid allocations, and remaining balances."""
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="آمار مالی آهنگ‌های هنرمند",
        description=(
            "لیست صفحه‌بندی‌شده آهنگ‌ها با کل درآمد، مبلغ تسویه‌شده، مبلغ در انتظار "
            "و مانده قابل تسویه. مرتب‌سازی پیش‌فرض بر اساس بیشترین مانده قابل تسویه است."
        ),
        parameters=[
            OpenApiParameter(
                "sort",
                OpenApiTypes.STR,
                description="مرتب‌سازی: available (پیش‌فرض)، remaining، income یا release_date",
            ),
            OpenApiParameter("page", OpenApiTypes.INT, description="شماره صفحه"),
            OpenApiParameter("page_size", OpenApiTypes.INT, description="تعداد نتیجه در هر صفحه؛ حداکثر ۱۰۰"),
        ],
        responses={200: SongSerializer(many=True)},
    )
    def get(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)

        artist = getattr(user, 'artist_profile', None)
        if not artist:
            return Response({"error": "Artist profile not found"}, status=status.HTTP_404_NOT_FOUND)

        sort = request.query_params.get('sort', 'available').lower()
        allowed_sorts = {'available', 'remaining', 'income', 'release_date'}
        if sort not in allowed_sorts:
            return Response(
                {"error": "Invalid sort. Use available, remaining, income, or release_date."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        songs = list(Song.objects.filter(artist=artist).select_related(
            'artist', 'album', 'uploader'
        ).prefetch_related(
            'featured_artists', 'genres', 'sub_genres', 'moods', 'tags'
        ).annotate(
            play_counts_count=Count('play_counts', distinct=True),
            paid_plays=Count('play_counts', filter=Q(play_counts__pay__gt=0), distinct=True),
            zero_value_plays=Count('play_counts', filter=Q(play_counts__pay=0), distinct=True),
            income=Coalesce(Sum('play_counts__pay'), _finance_zero()),
        ).annotate(
            total_plays=ExpressionWrapper(F('plays') + F('play_counts_count'), output_field=BigIntegerField()),
        ))

        song_totals = {
            song.id: _finance_decimal(getattr(song, 'income', 0))
            for song in songs
        }
        payout_requests = DepositRequest.objects.filter(
            artist=artist,
            status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_DONE],
        ).order_by('submission_date', 'pk')
        reserved_by_song, deposited_by_song, pending_by_song = _finance_artist_song_allocations(
            artist,
            song_totals=song_totals,
            requests=payout_requests,
        )

        records = []
        for song in songs:
            total_income = song_totals.get(song.id, Decimal('0'))
            deposited_income = min(total_income, deposited_by_song.get(song.id, Decimal('0')))
            pending_income = min(
                max(Decimal('0'), total_income - deposited_income),
                pending_by_song.get(song.id, Decimal('0')),
            )
            remaining_income = max(Decimal('0'), total_income - deposited_income)
            available_income = max(Decimal('0'), total_income - reserved_by_song.get(song.id, Decimal('0')))
            records.append({
                'song': song,
                'total_income': total_income,
                'deposited_income': deposited_income,
                'pending_income': pending_income,
                'remaining_income': remaining_income,
                'available_income': available_income,
            })

        if sort == 'release_date':
            records.sort(
                key=lambda item: (
                    item['song'].release_date or date.min,
                    item['available_income'],
                    item['total_income'],
                    item['song'].id,
                ),
                reverse=True,
            )
        else:
            metric = {
                'available': 'available_income',
                'remaining': 'remaining_income',
                'income': 'total_income',
            }[sort]
            records.sort(
                key=lambda item: (
                    item[metric],
                    item['remaining_income'],
                    item['total_income'],
                    int(getattr(item['song'], 'total_plays', 0) or 0),
                    item['song'].id,
                ),
                reverse=True,
            )

        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(records, request)
        page_records = page if page is not None else records
        page_songs = [record['song'] for record in page_records]
        hydrate_song_metrics(page_songs, request.user)
        serialized = SongSerializer(page_songs, many=True, context={'request': request}).data

        results = []
        for record, song_data in zip(page_records, serialized):
            song_obj = record['song']
            tracked_plays = int(getattr(song_obj, 'play_counts_count', 0) or 0)
            results.append({
                **song_data,
                'income': _finance_string(record['total_income']),
                'total_income': _finance_string(record['total_income']),
                'deposited_income': _finance_string(record['deposited_income']),
                'pending_income': _finance_string(record['pending_income']),
                'remaining_income': _finance_string(record['remaining_income']),
                'available_income': _finance_string(record['available_income']),
                'total_plays': int(getattr(song_obj, 'total_plays', 0) or 0),
                'tracked_plays': tracked_plays,
                'paid_plays': int(getattr(song_obj, 'paid_plays', 0) or 0),
                'zero_value_plays': int(getattr(song_obj, 'zero_value_plays', 0) or 0),
                'average_revenue_per_play': _finance_string(
                    (record['total_income'] / tracked_plays) if tracked_plays else 0
                ),
            })

        if page is not None:
            return paginator.get_paginated_response(results)
        return Response(results)


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistSettingsView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    PROFILE_FIELDS = (
        'name', 'name_en', 'artistic_name', 'artistic_name_en', 'email',
        'city', 'city_en', 'date_of_birth', 'address', 'address_en',
        'id_number', 'bio', 'bio_en',
    )
    SOCIAL_NAMES = {
        'instagram': ('اینستاگرام', 'Instagram'),
        'twitter': ('توییتر', 'Twitter'),
        'youtube': ('یوتیوب', 'YouTube'),
        'telegram': ('تلگرام', 'Telegram'),
    }

    def get_artist(self, user):
        if User.ROLE_ARTIST not in user.roles:
            return None
        return getattr(user, 'artist_profile', None)

    def get(self, request):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)
        return Response(ArtistSerializer(artist, context={'request': request}).data)

    def put(self, request):
        return self._update(request, partial=False)

    def patch(self, request):
        return self._update(request, partial=True)

    def _parse_social_accounts(self, raw_social):
        if raw_social is None:
            return None, None
        try:
            social_map = json.loads(raw_social) if isinstance(raw_social, str) else raw_social
            if not isinstance(social_map, dict):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            return None, {'social_accounts': ['Invalid social accounts payload.']}

        validator = URLValidator(schemes=['http', 'https'])
        normalized = {}
        for raw_slug, raw_url in social_map.items():
            slug = str(raw_slug).strip().lower()
            if slug not in self.SOCIAL_NAMES:
                continue
            url = str(raw_url or '').strip()
            if url:
                try:
                    validator(url)
                except ValidationError:
                    return None, {'social_accounts': [f'Invalid {slug} URL.']}
            normalized[slug] = url
        return normalized, None

    def _validate_upload(self, upload, field, max_size):
        if not upload:
            return None
        if upload.size > max_size:
            return {field: [f"File is too large. Maximum size is {max_size // (1024 * 1024)}MB."]}
        if getattr(upload, 'content_type', '') not in {'image/jpeg', 'image/png', 'image/webp'}:
            return {field: ["Only JPG, PNG, and WEBP images are supported."]}
        return None

    def _update(self, request, partial=True):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        profile_data = {}
        for field in self.PROFILE_FIELDS:
            if field not in request.data:
                continue
            value = request.data.get(field)
            if field == 'date_of_birth' and value in (None, '', 'null'):
                value = None
            profile_data[field] = value

        serializer = ArtistSerializer(
            artist,
            data=profile_data,
            partial=partial,
            context={'request': request},
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        social_map, social_error = self._parse_social_accounts(request.data.get('social_accounts'))
        if social_error:
            return Response(social_error, status=status.HTTP_400_BAD_REQUEST)

        uploads = {
            'profile_image': (request.FILES.get('profile_image'), 'artists/profiles', 5 * 1024 * 1024),
            'banner_image': (request.FILES.get('banner_image'), 'artists/banners', 10 * 1024 * 1024),
        }
        for field, (upload, _, max_size) in uploads.items():
            upload_error = self._validate_upload(upload, field, max_size)
            if upload_error:
                return Response(upload_error, status=status.HTTP_400_BAD_REQUEST)

        uploaded_urls = {}
        for field, (upload, folder, _) in uploads.items():
            if not upload:
                continue
            try:
                uploaded_urls[field], _ = upload_file_to_r2(upload, folder=folder, custom_filename=None)
            except Exception:
                return Response(
                    {'error': f"{field.replace('_', ' ').title()} upload failed. Please try again."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

        with transaction.atomic():
            artist = serializer.save()
            if uploaded_urls:
                for field, url in uploaded_urls.items():
                    setattr(artist, field, url)
                artist.save(update_fields=list(uploaded_urls))

            if social_map is not None:
                for slug, url in social_map.items():
                    name, name_en = self.SOCIAL_NAMES[slug]
                    platform, _ = SocialPlatform.objects.get_or_create(
                        slug=slug,
                        defaults={'name': name, 'name_en': name_en},
                    )
                    if not url:
                        ArtistSocialAccount.objects.filter(artist=artist, platform=platform).delete()
                    else:
                        ArtistSocialAccount.objects.update_or_create(
                            artist=artist,
                            platform=platform,
                            defaults={'url': url, 'username': ''},
                        )

        artist.refresh_from_db()
        return Response(ArtistSerializer(artist, context={'request': request}).data)


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user = request.user
        if User.ROLE_ARTIST not in user.roles:
            return Response({"error": "User is not an artist"}, status=status.HTTP_403_FORBIDDEN)
        current = str(request.data.get('current_password') or '')
        new = str(request.data.get('new_password') or '')
        if not current or not new:
            return Response({"error": "Current password and new password are required."}, status=status.HTTP_400_BAD_REQUEST)
        if not user.check_artist_password(current):
            return Response({"error": "Current password is incorrect."}, status=status.HTTP_400_BAD_REQUEST)
        if len(new) < 8:
            return Response({"error": "New password must be at least 8 characters long."}, status=status.HTTP_400_BAD_REQUEST)
        if current == new:
            return Response({"error": "New password must be different from the current password."}, status=status.HTTP_400_BAD_REQUEST)
        user.set_artist_password(new)
        user.save(update_fields=['artist_password'])
        return Response({"status": "password_changed", "message": "Password changed successfully."})


@extend_schema(tags=['Artist App Endpoints اندپوینت های اپلیکیشن هنرمند'])
class ArtistSongUploadStatusView(APIView):
    """Recover an upload result when an intermediary drops the original response."""
    permission_classes = [IsAuthenticated]

    def get(self, request, upload_id):
        token = _artist_upload_id(upload_id)
        if not token:
            return Response({'detail': 'Invalid upload identifier.', 'code': 'invalid_upload_id'}, status=status.HTTP_400_BAD_REQUEST)

        state = _get_artist_upload_state(request.user.id, token)
        if not state:
            return Response({'state': 'missing'}, status=status.HTTP_404_NOT_FOUND)

        if state.get('state') != 'done':
            return Response(state)

        try:
            artist = request.user.artist_profile
        except Artist.DoesNotExist:
            return Response({'detail': 'Artist profile not found.', 'code': 'artist_not_found'}, status=status.HTTP_404_NOT_FOUND)

        song = Song.objects.filter(pk=state.get('song_id'), artist=artist).select_related(
            'artist', 'album', 'uploader'
        ).prefetch_related(
            'featured_artists', 'genres', 'sub_genres', 'moods', 'tags',
            _artist_panel_release_links_prefetch(),
        ).first()
        if not song:
            return Response({'state': 'failed', 'detail': 'The uploaded recording could not be found.', 'code': 'upload_result_missing'})

        return Response({
            **state,
            'song': _apply_release_cover_fallback(
                song,
                dict(SongSerializer(song, context={'request': request}).data),
            ),
        })


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

        queryset = Song.objects.filter(artist=artist).select_related(
            'artist', 'album', 'uploader'
        ).prefetch_related(
            'featured_artists', 'genres', 'sub_genres', 'moods', 'tags',
            _artist_panel_release_links_prefetch(),
        ).annotate(
            album_active_songs_count_value=Count(
                'album__songs',
                filter=~Q(album__songs__status=Song.STATUS_DELETED),
                distinct=True,
            )
        )

        if pk:
            song = get_object_or_404(queryset, pk=pk)
            hydrate_song_metrics([song], request.user)

            try:
                days = max(1, min(int(request.query_params.get('days', 30)), 365))
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

            data = _apply_release_cover_fallback(
                song,
                dict(SongSerializer(song, context={'request': request}).data),
            )
            data['analytics'] = {
                'days': days,
                'total_period_plays': total_period_plays,
                'daily_plays': list(daily_plays),
                'city_distribution': city_data,
                'country_distribution': country_data
            }
            return Response(data)

        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(
                Q(title__icontains=query) | Q(title_en__icontains=query) |
                Q(album__title__icontains=query)
            ).distinct()

        queryset = queryset.order_by('-release_date', '-created_at')

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
            hydrate_song_metrics(page, request.user)
            return paginator.get_paginated_response(_serialize_artist_songs(page, request))

        songs = list(queryset)
        hydrate_song_metrics(songs, request.user)
        return Response(_serialize_artist_songs(songs, request))

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
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        raw_upload_id = request.data.get('upload_id')
        upload_id = _artist_upload_id(raw_upload_id)
        if raw_upload_id and not upload_id:
            return Response({'upload_id': ['Invalid upload identifier.']}, status=status.HTTP_400_BAD_REQUEST)

        previous = _get_artist_upload_state(request.user.id, upload_id)
        if previous and previous.get('state') == 'done':
            existing = Song.objects.filter(pk=previous.get('song_id'), artist=artist).first()
            if existing:
                return Response({
                    'message': 'OK',
                    'recovered': True,
                    'song': _apply_release_cover_fallback(
                        existing,
                        dict(SongSerializer(existing, context={'request': request}).data),
                    ),
                })
        if previous and previous.get('state') == 'processing':
            return Response(
                {'detail': 'This upload is already being processed.', 'code': 'upload_processing'},
                status=status.HTTP_409_CONFLICT,
            )

        audio_file = request.FILES.get('audio_file')
        cover_image = request.FILES.get('cover_image')
        title = str(request.data.get('title') or '').strip()
        if not title:
            return Response({'title': ['This field is required.']}, status=status.HTTP_400_BAD_REQUEST)
        if not audio_file:
            return Response({'audio_file': ['Audio file is required.']}, status=status.HTTP_400_BAD_REQUEST)
        if audio_file.size > 500 * 1024 * 1024:
            return Response({'audio_file': ['Audio file must be smaller than 500MB.']}, status=status.HTTP_400_BAD_REQUEST)
        extension = os.path.splitext(audio_file.name or '')[1].lower()
        if extension not in {'.mp3', '.wav'}:
            return Response({'audio_file': ['Only MP3 and WAV audio files are supported.']}, status=status.HTTP_400_BAD_REQUEST)
        if cover_image:
            if cover_image.size > 10 * 1024 * 1024:
                return Response({'cover_image': ['Cover image must be smaller than 10MB.']}, status=status.HTTP_400_BAD_REQUEST)
            if getattr(cover_image, 'content_type', '') not in {'image/jpeg', 'image/png', 'image/webp'}:
                return Response({'cover_image': ['Cover image must be JPG, PNG, or WEBP.']}, status=status.HTTP_400_BAD_REQUEST)

        clean = {}
        scalar_fields = [
            'title', 'title_en', 'is_single', 'release_date', 'language', 'description', 'description_en',
            'lyrics', 'lyrics_en', 'tempo', 'energy', 'danceability', 'valence', 'acousticness',
            'instrumentalness', 'speechiness', 'live_performed', 'label', 'label_en', 'credits', 'credits_en',
        ]
        for field in scalar_fields:
            if field in request.data:
                clean[field] = request.data.get(field)

        for field in ['producers', 'producers_en', 'composers', 'composers_en', 'lyricists', 'lyricists_en']:
            raw = request.data.getlist(field) if hasattr(request.data, 'getlist') else request.data.get(field)
            if raw:
                values = raw if isinstance(raw, list) else [raw]
                clean[field] = _clean_string_list([
                    part.strip() for item in values for part in str(item).split(',')
                ])

        featured_ids = []
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids', 'featured_artist_ids']:
            raw = request.data.getlist(field) if hasattr(request.data, 'getlist') else request.data.get(field)
            normalized = _normalize_id_list(raw)
            if normalized is None:
                continue
            if field == 'featured_artist_ids':
                featured_ids = [value for value in normalized if value != artist.id]
                clean['featured_artist_ids'] = featured_ids
            else:
                clean[f'{field}_write'] = normalized

        # Validate all metadata and relationships before any external upload.
        save_as_draft = str(request.data.get('save_as_draft') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
        target_status = Song.STATUS_DRAFT if save_as_draft else Song.STATUS_PENDING
        preflight = {
            **clean,
            'audio_file': 'https://example.com/preflight-audio.mp3',
            'cover_image': 'https://example.com/preflight-cover.jpg' if cover_image else '',
            'uploader': request.user.id,
            'status': target_status,
        }
        preflight_serializer = SongSerializer(data=preflight, context={'request': request})
        if not preflight_serializer.is_valid():
            return Response(preflight_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        artist_name = artist_filename_name(artist)
        filename_title = str(clean.get('title_en') or title).strip()
        filename_base = f"{artist_name} - {filename_title}"

        uploaded_urls = []
        stage = 'audio_file'
        _set_artist_upload_state(
            request.user.id,
            upload_id,
            'processing',
            stage='analyzing',
            message='The server is validating and processing the audio.',
        )

        def report_stage(current_stage):
            messages = {
                'analyzing': 'The server is validating the audio.',
                'converting_128': 'The server is creating the 128 kbps version.',
                'uploading_r2': 'The server is storing both audio qualities in R2.',
                'saving': 'The server is saving the recording.',
            }
            _set_artist_upload_state(
                request.user.id,
                upload_id,
                'processing',
                stage=current_stage,
                message=messages.get(current_stage, 'The server is processing the recording.'),
            )

        try:
            variants = upload_audio_variants(audio_file, filename_base, stage_callback=report_stage)
            uploaded_urls.extend(filter(None, [variants['audio_file'], variants['converted_audio_url']]))

            cover_url = ''
            if cover_image:
                stage = 'cover_image'
                cover_url, _ = upload_file_to_r2(cover_image, folder='covers')
                uploaded_urls.append(cover_url)

            clean.update({
                'audio_file': variants['audio_file'],
                'converted_audio_url': variants['converted_audio_url'],
                'cover_image': cover_url,
                'original_format': variants['original_format'],
                'duration_seconds': variants['duration_seconds'],
                'uploader': request.user.id,
                'status': target_status,
            })
            stage = 'detail'
            serializer = SongSerializer(data=clean, context={'request': request})
            if not serializer.is_valid():
                cleanup_r2_urls(uploaded_urls)
                _set_artist_upload_state(
                    request.user.id,
                    upload_id,
                    'failed',
                    detail='The recording metadata could not be saved.',
                    code='song_validation_failed',
                )
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            with transaction.atomic():
                song = serializer.save(artist=artist)
            _set_artist_upload_state(
                request.user.id,
                upload_id,
                'done',
                stage='done',
                message='The recording was uploaded and processed successfully.',
                song_id=song.id,
            )
        except MediaPipelineError as exc:
            cleanup_r2_urls(uploaded_urls)
            _set_artist_upload_state(
                request.user.id,
                upload_id,
                'failed',
                detail=str(exc),
                code=exc.code,
            )
            payload = {stage: [str(exc)], 'code': exc.code} if stage != 'detail' else {'detail': str(exc), 'code': exc.code}
            return Response(payload, status=exc.status_code)
        except Exception:
            cleanup_r2_urls(uploaded_urls)
            logger.exception('Artist song upload failed for user=%s upload_id=%s stage=%s', request.user.pk, upload_id, stage)
            _set_artist_upload_state(
                request.user.id,
                upload_id,
                'failed',
                detail='The song could not be saved after upload.',
                code='song_save_failed',
            )
            return Response(
                {'detail': 'The song could not be saved after upload.', 'code': 'song_save_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response({"message": "OK", "song": serializer.data}, status=status.HTTP_201_CREATED)

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
        if song.status == Song.STATUS_DELETED:
            return Response({
                'detail': 'Deleted recordings are read-only so their stream and payment history stays intact.'
            }, status=status.HTTP_409_CONFLICT)
        linked_release_rows = list(
            song.release_track_links.select_related('release').order_by('release_id')
        )
        requires_reapproval = (
            song.status in {Song.STATUS_APPROVED, Song.STATUS_PUBLISHED}
            or any(link.release.status not in {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW} for link in linked_release_rows)
        )
        confirmed_reapproval = str(request.data.get('confirm_re_review') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
        if requires_reapproval and not confirmed_reapproval:
            return Response({
                'detail': 'Saving these changes will return the song and its release to pending review.',
                'code': 'release_reapproval_required',
                'release_ids': [str(link.release_id) for link in linked_release_rows],
            }, status=status.HTTP_409_CONFLICT)

        data = {}
        scalar_fields = {
            'title', 'title_en', 'is_single', 'release_date', 'language', 'description', 'description_en',
            'lyrics', 'lyrics_en', 'tempo', 'energy', 'danceability', 'valence', 'acousticness',
            'instrumentalness', 'speechiness', 'live_performed', 'label', 'label_en', 'credits', 'credits_en',
        }
        list_fields = {
            'genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids', 'featured_artist_ids',
            'producers', 'producers_en', 'composers', 'composers_en', 'lyricists', 'lyricists_en',
        }
        for key in scalar_fields:
            if key in request.data:
                data[key] = request.data.get(key)
        for key in list_fields:
            if key in request.data:
                data[key] = request.data.getlist(key) if hasattr(request.data, 'getlist') else request.data.get(key)

        # Map user-friendly field names to serializer write_only fields
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids', 'featured_artist_ids']:
            if field in data:
                raw_value = data.get(field)
                normalized = _normalize_id_list(raw_value)
                normalized = normalized if normalized is not None else []
                if field == 'featured_artist_ids':
                    data['featured_artist_ids'] = [value for value in normalized if value != artist.id]
                else:
                    data[f"{field}_write"] = normalized
                    data.pop(field, None)
        for field in ['producers', 'producers_en', 'composers', 'composers_en', 'lyricists', 'lyricists_en']:
            if field in data:
                raw = data[field] if isinstance(data[field], list) else [data[field]]
                data[field] = _clean_string_list([part.strip() for item in raw for part in str(item).split(',')])

        audio_file = request.FILES.get('audio_file')
        cover_image = request.FILES.get('cover_image')
        if audio_file:
            if audio_file.size > 500 * 1024 * 1024:
                return Response({'audio_file': ['Audio file must be smaller than 500MB.']}, status=status.HTTP_400_BAD_REQUEST)
            if os.path.splitext(audio_file.name or '')[1].lower() not in {'.mp3', '.wav'}:
                return Response({'audio_file': ['Only MP3 and WAV audio files are supported.']}, status=status.HTTP_400_BAD_REQUEST)
        if cover_image:
            if cover_image.size > 10 * 1024 * 1024:
                return Response({'cover_image': ['Cover image must be smaller than 10MB.']}, status=status.HTTP_400_BAD_REQUEST)
            if getattr(cover_image, 'content_type', '') not in {'image/jpeg', 'image/png', 'image/webp'}:
                return Response({'cover_image': ['Cover image must be JPG, PNG, or WEBP.']}, status=status.HTTP_400_BAD_REQUEST)

        # Validate metadata and relation changes before uploading replacement files.
        save_as_draft = str(request.data.get('save_as_draft') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
        target_status = Song.STATUS_DRAFT if save_as_draft else Song.STATUS_PENDING
        preflight_data = {**data, 'status': target_status}
        if audio_file:
            preflight_data['audio_file'] = 'https://example.com/preflight-audio.mp3'
        if cover_image:
            preflight_data['cover_image'] = 'https://example.com/preflight-cover.jpg'
        preflight_serializer = SongSerializer(song, data=preflight_data, partial=partial, context={'request': request})
        if not preflight_serializer.is_valid():
            return Response(preflight_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        new_urls = []
        old_urls = []
        stage = 'audio_file'
        try:
            if audio_file:
                title = str(data.get('title', song.title) or '').strip()
                title_en = str(data['title_en'] if 'title_en' in data else song.title_en or '').strip()
                filename_base = f"{artist_filename_name(artist)} - {title_en or title}"

                variants = upload_audio_variants(audio_file, filename_base)
                new_urls.extend(filter(None, [variants['audio_file'], variants['converted_audio_url']]))
                old_urls.extend(filter(None, [song.audio_file, song.converted_audio_url]))
                data.update({
                    'audio_file': variants['audio_file'],
                    'converted_audio_url': variants['converted_audio_url'],
                    'duration_seconds': variants['duration_seconds'],
                    'original_format': variants['original_format'],
                })

            if cover_image:
                stage = 'cover_image'
                cover_url, _ = upload_file_to_r2(cover_image, folder='covers')
                new_urls.append(cover_url)
                if song.cover_image:
                    old_urls.append(song.cover_image)
                data['cover_image'] = cover_url

            stage = 'detail'
            data['status'] = target_status
            with transaction.atomic():
                release_ids = list(
                    ArtistRelease.objects.filter(release_tracks__song_id=song.pk)
                    .values_list('pk', flat=True)
                    .distinct()
                )
                linked_releases = list(
                    ArtistRelease.objects.select_for_update()
                    .filter(pk__in=release_ids)
                    .order_by('pk')
                )
                now_requires_reapproval = (
                    song.status in {Song.STATUS_APPROVED, Song.STATUS_PUBLISHED}
                    or any(item.status not in {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW} for item in linked_releases)
                )
                if now_requires_reapproval and not confirmed_reapproval:
                    cleanup_r2_urls(new_urls)
                    return Response({
                        'detail': 'The song or release status changed while editing. Confirm review again and retry.',
                        'code': 'release_reapproval_required',
                    }, status=status.HTTP_409_CONFLICT)

                locked_song = Song.objects.select_for_update().get(pk=song.pk, artist=artist)
                serializer = SongSerializer(locked_song, data=data, partial=partial, context={'request': request})
                if not serializer.is_valid():
                    cleanup_r2_urls(new_urls)
                    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
                saved_song = serializer.save()
                for linked_release in linked_releases:
                    _sync_release_from_artist_song(linked_release, saved_song, cover_changed=bool(cover_image))
                    if linked_release.status not in {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW}:
                        mark_release_for_review(
                            linked_release, actor=request.user, all_tracks=True,
                            note='Artist edited release-owned song metadata; approval is required again.',
                        )
                    elif linked_release.status == ArtistRelease.STATUS_IN_REVIEW:
                        Song.objects.filter(release_track_links__release=linked_release).exclude(
                            status=Song.STATUS_DELETED
                        ).update(status=Song.STATUS_PENDING)
                        ArtistRelease.objects.filter(pk=linked_release.pk).update(
                            validation_snapshot={}, lock_version=F('lock_version') + 1, updated_at=timezone.now(),
                        )
                    else:
                        ArtistRelease.objects.filter(pk=linked_release.pk).update(
                            validation_snapshot={}, lock_version=F('lock_version') + 1, updated_at=timezone.now(),
                        )
        except MediaPipelineError as exc:
            cleanup_r2_urls(new_urls)
            payload = {stage: [str(exc)], 'code': exc.code} if stage != 'detail' else {'detail': str(exc), 'code': exc.code}
            return Response(payload, status=exc.status_code)
        except Exception:
            cleanup_r2_urls(new_urls)
            logger.exception('Artist song update failed song=%s user=%s', song.pk, request.user.pk)
            return Response(
                {'detail': 'The recording update failed and no replacement file was kept.', 'code': 'song_update_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        _cleanup_unreferenced_song_media(old_urls)
        return Response({"message": "OK", "song": serializer.data})

    def delete(self, request, pk=None):
        """Delete a recording while retaining rows needed by releases and accounting."""
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        media_urls = []
        album_deleted = False
        album_deletion = None
        with transaction.atomic():
            song_ref = get_object_or_404(Song.objects.only('pk', 'album_id'), pk=pk, artist=artist)
            album = None
            if song_ref.album_id:
                album = Album.objects.select_for_update().filter(pk=song_ref.album_id, artist=artist).first()
            song = Song.objects.select_for_update().get(pk=song_ref.pk, artist=artist)
            deletion, removed_media = _delete_artist_song_locked(song, actor=request.user)
            media_urls.extend(removed_media)

            if album and not album.songs.exclude(status=Song.STATUS_DELETED).exists():
                album_deleted = True
                if album.songs.exists():
                    album_deletion = 'soft'
                else:
                    album_deletion = 'hard'
                    if album.cover_image:
                        media_urls.append(album.cover_image)
                    album.delete()

            if media_urls:
                transaction.on_commit(lambda values=tuple(media_urls): _cleanup_unreferenced_song_media(values))

            payload = {
                "message": "OK",
                "deletion": deletion,
                "album_deleted": album_deleted,
                "album_deletion": album_deletion,
            }
            if deletion == 'soft':
                payload['song'] = _apply_release_cover_fallback(
                    song,
                    dict(SongSerializer(song, context={'request': request}).data),
                )
            return Response(payload)


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

        songs_qs = Song.objects.select_related('artist', 'album', 'uploader').prefetch_related(
            'featured_artists', 'genres', 'sub_genres', 'moods', 'tags'
        ).annotate(
            artist_tracked_plays=Count('play_counts', distinct=True),
            artist_income=Coalesce(Sum('play_counts__pay'), _finance_zero()),
        ).order_by('id')
        albums_qs = Album.objects.filter(artist=artist).prefetch_related(
            'genres', 'sub_genres', 'moods', Prefetch('songs', queryset=songs_qs)
        )

        if pk:
            album = get_object_or_404(albums_qs, pk=pk)
            hydrate_album_metrics([album], request.user)
            hydrate_song_metrics(album.songs.all(), request.user)
            tracks = list(album.songs.all())
            data = _artist_album_payload(
                album,
                AlbumSerializer(album, context={'request': request}).data,
                tracks,
            )
            data['songs'] = SongSerializer(tracks, many=True, context={'request': request}).data
            return Response(data)

        queryset = albums_qs.order_by('-release_date', '-created_at')
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(queryset, request)
        if page is not None:
            hydrate_album_metrics(page, request.user)
            hydrate_song_metrics([song for album in page for song in album.songs.all()], request.user)
            serializer = AlbumSerializer(page, many=True, context={'request': request})
            results = [
                _artist_album_payload(album, item, list(album.songs.all()))
                for album, item in zip(page, serializer.data)
            ]
            return paginator.get_paginated_response(results)

        albums = list(queryset)
        hydrate_album_metrics(albums, request.user)
        hydrate_song_metrics([song for album in albums for song in album.songs.all()], request.user)
        serializer = AlbumSerializer(albums, many=True, context={'request': request})
        return Response([
            _artist_album_payload(album, item, list(album.songs.all()))
            for album, item in zip(albums, serializer.data)
        ])

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
        if not str(request.data.get('title') or '').strip():
            return Response({'title': ['This field is required.']}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Create Album. Only pass album fields to the album serializer;
        # nested song fields are processed separately below.
        album_data = {
            field: request.data.get(field)
            for field in ('title', 'title_en', 'release_date', 'description', 'description_en')
            if field in request.data
        }
        for field in ('genre_ids', 'sub_genre_ids', 'mood_ids'):
            if field in request.data:
                album_data[field] = request.data.getlist(field) if hasattr(request.data, 'getlist') else request.data.get(field)

        raw_existing_song_ids = request.data.getlist('existing_song_ids') if hasattr(request.data, 'getlist') else request.data.get('existing_song_ids')
        existing_song_ids = _normalize_id_list(raw_existing_song_ids) or []
        available_song_ids = set(Song.objects.filter(
            id__in=existing_song_ids, artist=artist, album__isnull=True
        ).exclude(status=Song.STATUS_DELETED).values_list('id', flat=True))
        unavailable_song_ids = [song_id for song_id in existing_song_ids if song_id not in available_song_ids]
        if unavailable_song_ids:
            return Response({
                'existing_song_ids': [f"Songs are unavailable or do not belong to this artist: {unavailable_song_ids}"]
            }, status=status.HTTP_400_BAD_REQUEST)

        # Map user-friendly field names to serializer write_only fields for album
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids']:
            if field in album_data and f"{field}_write" not in album_data:
                raw_val = album_data.get(field)
                normalized = _normalize_id_list(raw_val)
                album_data[f"{field}_write"] = normalized if normalized is not None else []
                album_data.pop(field, None)

        album_serializer = AlbumSerializer(data=album_data, context={'request': request})
        if not album_serializer.is_valid():
            return Response(album_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        album_cover = request.FILES.get('cover_image')
        cover_url = ''
        if album_cover:
            if album_cover.size > 10 * 1024 * 1024:
                return Response({'cover_image': ['Album cover must be smaller than 10MB.']}, status=status.HTTP_400_BAD_REQUEST)
            if getattr(album_cover, 'content_type', '') not in {'image/jpeg', 'image/png', 'image/webp'}:
                return Response({'cover_image': ['Album cover must be JPG, PNG, or WEBP.']}, status=status.HTTP_400_BAD_REQUEST)
            safe_title = make_safe_filename(album_data.get('title_en') or album_data.get('title') or 'album')
            safe_artist = make_safe_filename(artist_filename_name(artist))
            cover_filename = f"{safe_artist} - {safe_title}_album_cover"
            try:
                cover_url, _ = upload_file_to_r2(album_cover, folder='covers', custom_filename=cover_filename)
            except Exception:
                return Response({'cover_image': ['Album cover upload failed. Please try again.']}, status=status.HTTP_502_BAD_GATEWAY)

        save_kwargs = {'artist': artist}
        if cover_url:
            save_kwargs['cover_image'] = cover_url
        album = album_serializer.save(**save_kwargs)

        # 2. Process Songs
        if existing_song_ids:
            Song.objects.filter(id__in=existing_song_ids, artist=artist, album__isnull=True).update(
                album=album, is_single=False
            )

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
            artist_name = artist_filename_name(artist)
            duration, bitrate, format_ext = get_audio_info(audio_file)
            if not format_ext:
                _, ext = os.path.splitext(audio_file.name)
                format_ext = ext.lstrip('.').lower()

            # Build filename base
            # Note: featured artists for individual songs in album creation might not be supported in the current form structure,
            # but we'll use the artist name and title.
            filename_title = str(request.data.get(f"{prefix}title_en") or title).strip()
            filename_base = f"{artist_name} - {filename_title}"
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
                'status': Song.STATUS_PENDING,
                'title_en': request.data.get(f"{prefix}title_en", ""),
                'lyrics': request.data.get(f"{prefix}lyrics", ""),
                'lyrics_en': request.data.get(f"{prefix}lyrics_en", ""),
                'description': request.data.get(f"{prefix}description", ""),
                'description_en': request.data.get(f"{prefix}description_en", ""),
                'release_date': album.release_date,
                'language': request.data.get(f"{prefix}language", "fa"),
            }

            # Handle JSON fields
            for list_field in ['producers', 'producers_en', 'composers', 'composers_en', 'lyricists', 'lyricists_en']:
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
        if _album_is_deleted(album):
            return Response({'detail': 'Deleted albums are read-only so historical track and payment records remain stable.'}, status=status.HTTP_409_CONFLICT)

        album_data = {
            field: request.data.get(field)
            for field in ('title', 'title_en', 'release_date', 'description', 'description_en')
            if field in request.data
        }
        for field in ('genre_ids', 'sub_genre_ids', 'mood_ids'):
            if field in request.data:
                album_data[field] = request.data.getlist(field) if hasattr(request.data, 'getlist') else request.data.get(field)

        replace_song_ids = None
        if 'existing_song_ids' in request.data:
            raw_song_ids = request.data.getlist('existing_song_ids') if hasattr(request.data, 'getlist') else request.data.get('existing_song_ids')
            replace_song_ids = _normalize_id_list(raw_song_ids) or []
            allowed_song_ids = set(Song.objects.filter(
                Q(album__isnull=True) | Q(album=album),
                id__in=replace_song_ids,
                artist=artist,
            ).exclude(status=Song.STATUS_DELETED).values_list('id', flat=True))
            unavailable_song_ids = [song_id for song_id in replace_song_ids if song_id not in allowed_song_ids]
            if unavailable_song_ids:
                return Response({
                    'existing_song_ids': [f"Songs are unavailable or do not belong to this artist: {unavailable_song_ids}"]
                }, status=status.HTTP_400_BAD_REQUEST)

        # Map user-friendly field names to serializer write_only fields for album
        for field in ['genre_ids', 'sub_genre_ids', 'mood_ids']:
            if field in album_data and f"{field}_write" not in album_data:
                raw_val = album_data.get(field)
                normalized = _normalize_id_list(raw_val)
                album_data[f"{field}_write"] = normalized if normalized is not None else []
                album_data.pop(field, None)

        serializer = AlbumSerializer(album, data=album_data, partial=partial, context={'request': request})
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        album_cover = request.FILES.get('cover_image')
        cover_url = ''
        if album_cover:
            if album_cover.size > 10 * 1024 * 1024:
                return Response({'cover_image': ['Album cover must be smaller than 10MB.']}, status=status.HTTP_400_BAD_REQUEST)
            if getattr(album_cover, 'content_type', '') not in {'image/jpeg', 'image/png', 'image/webp'}:
                return Response({'cover_image': ['Album cover must be JPG, PNG, or WEBP.']}, status=status.HTTP_400_BAD_REQUEST)
            safe_title = make_safe_filename(
                album_data.get('title_en') or album.title_en or album_data.get('title') or album.title
            )
            safe_artist = make_safe_filename(artist_filename_name(artist))
            cover_filename = f"{safe_artist} - {safe_title}_album_cover"
            try:
                cover_url, _ = upload_file_to_r2(album_cover, folder='covers', custom_filename=cover_filename)
            except Exception:
                return Response({'cover_image': ['Album cover upload failed. Please try again.']}, status=status.HTTP_502_BAD_GATEWAY)

        with transaction.atomic():
            serializer.save(**({'cover_image': cover_url} if cover_url else {}))
            if replace_song_ids is not None:
                Song.objects.filter(album=album).exclude(status=Song.STATUS_DELETED).exclude(id__in=replace_song_ids).update(
                    album=None, is_single=True
                )
                if replace_song_ids:
                    Song.objects.filter(
                        Q(album__isnull=True) | Q(album=album),
                        id__in=replace_song_ids,
                        artist=artist,
                    ).update(album=album, is_single=False)

        album.refresh_from_db()
        response_tracks = list(
            Song.objects.filter(album=album).annotate(
                artist_tracked_plays=Count('play_counts', distinct=True),
                artist_income=Coalesce(Sum('play_counts__pay'), _finance_zero()),
            ).order_by('id')
        )
        response_data = _artist_album_payload(
            album,
            AlbumSerializer(album, context={'request': request}).data,
            response_tracks,
        )
        response_data['songs'] = SongSerializer(
            response_tracks,
            many=True,
            context={'request': request},
        ).data
        return Response({
            "message": "Album updated successfully",
            "album": response_data
        })

    @extend_schema(
        summary="حذف آلبوم",
        description="حذف آلبوم با حفظ ترک‌ها و سوابق مالی منتشرشده.",
        responses={200: None}
    )
    def delete(self, request, pk):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        media_urls = []
        soft_count = 0
        hard_count = 0
        with transaction.atomic():
            album = get_object_or_404(Album.objects.select_for_update(), pk=pk, artist=artist)
            linked_release_ids = list(
                ArtistRelease.objects.select_for_update()
                .filter(album=album, artist=artist)
                .values_list('pk', flat=True)
            )
            songs = list(Song.objects.select_for_update().filter(album=album).order_by('id'))
            for song in songs:
                deletion, removed_media = _delete_artist_song_locked(song, actor=request.user)
                media_urls.extend(removed_media)
                if deletion == 'soft':
                    soft_count += 1
                else:
                    hard_count += 1

            media_urls.extend(_mark_releases_without_active_tracks(linked_release_ids, actor=request.user))
            ArtistRelease.objects.filter(
                pk__in=linked_release_ids,
                status=ArtistRelease.STATUS_DRAFT,
                release_tracks__isnull=True,
            ).delete()

            if album.songs.exists():
                album.refresh_from_db()
                payload = {
                    'message': 'Album disabled; released recordings and accounting history were preserved.',
                    'deletion': 'soft',
                    'soft_deleted_tracks': soft_count,
                    'hard_deleted_tracks': hard_count,
                    'album': _artist_album_payload(
                        album,
                        AlbumSerializer(album, context={'request': request}).data,
                        list(album.songs.annotate(
                            artist_tracked_plays=Count('play_counts', distinct=True),
                            artist_income=Coalesce(Sum('play_counts__pay'), _finance_zero()),
                        )),
                    ),
                }
            else:
                if album.cover_image:
                    media_urls.append(album.cover_image)
                album.delete()
                payload = {
                    'message': 'Album and its disposable recordings were permanently deleted.',
                    'deletion': 'hard',
                    'soft_deleted_tracks': soft_count,
                    'hard_deleted_tracks': hard_count,
                }

            if media_urls:
                transaction.on_commit(lambda values=tuple(media_urls): _cleanup_unreferenced_song_media(values))
            return Response(payload)


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
        if _album_is_deleted(album):
            return Response({'detail': 'Deleted albums cannot accept new recordings.'}, status=status.HTTP_409_CONFLICT)

        raw = request.data.get('song_ids') or request.data.get('song_id') or request.data.getlist('song_ids')
        song_ids = _normalize_id_list(raw)
        if not song_ids:
            return Response({'error': 'song_ids is required (list of integers)'}, status=status.HTTP_400_BAD_REQUEST)

        qs = Song.objects.filter(
            Q(album__isnull=True) | Q(album=album),
            id__in=song_ids,
            artist=artist,
        ).exclude(status=Song.STATUS_DELETED)
        updated_ids = list(qs.values_list('id', flat=True))
        updated_count = qs.update(album=album, is_single=False)
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
        description="آهنگ‌ها را از آلبوم جدا می‌کند؛ اگر آخرین آهنگ فعال حذف شود، آلبوم نیز حذف/غیرفعال می‌شود.",
        request=inline_serializer(name='RemoveSongsFromAlbum', fields={
            'song_ids': serializers.ListField(child=serializers.IntegerField())
        }),
        responses={200: inline_serializer(name='RemoveFromAlbumResponse', fields={'removed_count': serializers.IntegerField()})}
    )
    def delete(self, request, pk):
        artist = self.get_artist(request.user)
        if not artist:
            return Response({"error": "Artist profile not found or user is not an artist"}, status=status.HTTP_404_NOT_FOUND)

        raw = request.data.get('song_ids') or request.data.get('song_id') or request.data.getlist('song_ids')
        song_ids = _normalize_id_list(raw)
        if not song_ids:
            return Response({'error': 'song_ids is required (list of integers)'}, status=status.HTTP_400_BAD_REQUEST)

        media_urls = []
        with transaction.atomic():
            album = get_object_or_404(Album.objects.select_for_update(), pk=pk, artist=artist)
            qs = Song.objects.select_for_update().filter(id__in=song_ids, artist=artist, album=album)
            removed_ids = list(qs.values_list('id', flat=True))
            release_links = list(
                ArtistReleaseTrack.objects.select_for_update()
                .filter(release__artist=artist, release__album=album, song_id__in=removed_ids)
            )
            release_ids = {link.release_id for link in release_links}
            if release_links:
                ArtistReleaseTrack.objects.filter(pk__in=[link.pk for link in release_links]).delete()
                _renumber_release_tracks(release_ids)
                ArtistRelease.objects.filter(pk__in=release_ids).update(
                    validation_snapshot={},
                    lock_version=F('lock_version') + 1,
                    updated_at=timezone.now(),
                )

            removed_count = qs.update(album=None, is_single=True)
            missing = [song_id for song_id in song_ids if song_id not in removed_ids]
            media_urls.extend(_mark_releases_without_active_tracks(release_ids, actor=request.user))

            album_deleted = not album.songs.exclude(status=Song.STATUS_DELETED).exists()
            album_deletion = None
            if album_deleted:
                if album.songs.exists():
                    album_deletion = 'soft'
                else:
                    album_deletion = 'hard'
                    if album.cover_image:
                        media_urls.append(album.cover_image)
                    album.delete()

            if media_urls:
                transaction.on_commit(lambda values=tuple(media_urls): _cleanup_unreferenced_song_media(values))
            return Response({
                'removed_count': removed_count,
                'removed_ids': removed_ids,
                'missing_or_not_owned_or_not_in_album': missing,
                'album_deleted': album_deleted,
                'album_deletion': album_deletion,
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
        description="دریافت هر اعلان خوانده‌نشده به‌صورت یک رکورد مستقل برای کاربر یا پنل هنرمند.",
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
        # Return each unread row independently.  The previous text/number based
        # grouping hid multiple database rows behind one id, so marking the
        # visible item as read left invisible unread notifications that returned
        # on the next refresh.
        return super().list(request, *args, **kwargs)


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
                    'message': serializers.CharField(),
                    'read_through_id': serializers.IntegerField(required=False, allow_null=True),
                }
            )
        }
    )
    def post(self, request, pk=None):
        user = request.user
        is_artist = request.query_params.get('artist', '').lower() == 'true'

        if is_artist:
            artist = getattr(user, 'artist_profile', None)
            if not artist:
                return Response(
                    {"error": "No artist profile found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            owned = Notification.objects.filter(artist=artist)
        else:
            owned = Notification.objects.filter(user=user)

        if pk is not None:
            # Idempotent and ownership-safe: unauthorized ids are indistinguishable
            # from missing ids and an already-read row still returns success.
            with transaction.atomic():
                if not is_artist:
                    # Notification producers lock this same user row. The lock
                    # gives read-vs-create races a deterministic commit order.
                    User.objects.select_for_update().only("id").get(pk=user.pk)
                notification = get_object_or_404(owned.select_for_update(), pk=pk)
                if not notification.has_read:
                    owned.filter(pk=pk, has_read=False).update(has_read=True)
                if not is_artist:
                    transaction.on_commit(
                        lambda user_id=user.pk, notification_id=notification.pk:
                        publish_notification_read(user_id, notification_id)
                    )
            return Response({"message": "Notification marked as read"})

        with transaction.atomic():
            if not is_artist:
                User.objects.select_for_update().only("id").get(pk=user.pk)
            read_through_id = owned.filter(has_read=False).aggregate(
                max_id=Max("id")
            )["max_id"]
            if read_through_id is not None:
                owned.filter(
                    has_read=False,
                    id__lte=read_through_id,
                ).update(has_read=True)
            if not is_artist:
                transaction.on_commit(
                    lambda user_id=user.pk, through=read_through_id:
                    publish_all_notifications_read(user_id, through)
                )
        return Response({
            "message": "All notifications marked as read",
            "read_through_id": read_through_id,
        })


@extend_schema(
    summary="Get premium plan price",
    description="Returns the current Premium plan price and currency for audience clients (GET only).",
    responses={200: OpenApiTypes.OBJECT}
)
class PremiumPlanPriceView(APIView):
    """Public endpoint that returns the Premium plan price."""
    permission_classes = [AllowAny]

    def get(self, request):
        fallback = float(getattr(settings, 'PREMIUM_PLAN_PRICE', 0))
        price_val = fallback
        try:
            config = PlayConfiguration.objects.order_by('-updated_at').only(
                'premium_plan_price'
            ).first()
            if config and config.premium_plan_price is not None:
                price_val = float(config.premium_plan_price)
        except Exception:
            # The public pricing screen should remain available during a brief
            # configuration-table outage or an incomplete deployment.
            price_val = fallback

        response = Response({
            'plan': 'premium',
            'price': price_val,
            'currency': 'TOMAN',
        }, status=status.HTTP_200_OK)
        response['Cache-Control'] = 'no-store, max-age=0'
        response['Pragma'] = 'no-cache'
        return response


@extend_schema(
    summary="Complete simulated premium checkout",
    description="Resets Premium to exactly 30 days from the successful payment time for the authenticated audience account.",
    request=inline_serializer(
        name='PremiumCheckoutRequest',
        fields={'gateway': serializers.ChoiceField(choices=['zarinpal'])},
    ),
    responses={200: UserSerializer},
)
class PremiumPlanActivateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        gateway = str(request.data.get('gateway') or '').strip().lower()
        if gateway != 'zarinpal':
            return Response(
                {
                    'error': {
                        'code': 'PAYMENT_GATEWAY_INVALID',
                        'message': 'The selected payment gateway is not available.',
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user, expiry = activate_one_month_premium_locked(
            request.user.pk, gateway=gateway
        )
        payload = UserSerializer(user, context={'request': request}).data
        response = Response(
            {
                'message': 'Premium activated successfully.',
                'plan': user.plan,
                'premium_expires_at': expiry.isoformat(),
                'user': payload,
            },
            status=status.HTTP_200_OK,
        )
        response['Cache-Control'] = 'private, no-store, max-age=0'
        response['Pragma'] = 'no-cache'
        response['Vary'] = 'Authorization, Accept-Language'
        return response
