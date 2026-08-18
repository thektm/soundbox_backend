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
    ArtistRelease, ArtistReleaseTrack, ArtistReleaseStatusHistory, ArtistSocialAccount, SocialPlatform, RefreshToken
)
from .models import PlayCount
from django.utils import timezone
from datetime import timedelta
from django.db import transaction, IntegrityError
from django.db.models import Sum, Count, Q, Case, When, Value, IntegerField, F, Prefetch
from decimal import Decimal
from .admin_serializers import (
    AdminUserSerializer, AdminArtistSerializer, AdminArtistAuthSerializer, 
    AdminSongSerializer, AdminReportSerializer, AdminAlbumSerializer,
    AdminPlayConfigurationSerializer, AdminBannerAdSerializer, AdminAudioAdSerializer,
    AdminPaymentTransactionSerializer, AdminDepositRequestSerializer,
    AdminSearchSectionSerializer, AdminEventPlaylistSerializer, AdminPlaylistSerializer,
    AdminEmployeeSerializer, AdminSupportTicketSerializer, AdminSongPromotionSerializer,
    AdminTaxonomySerializer
)
from rest_framework.parsers import MultiPartParser, FormParser
from .utils import upload_file_to_r2, convert_to_128kbps, get_audio_info, make_safe_filename, generate_signed_r2_url, check_r2_storage, cleanup_r2_urls
from .song_play_metrics import apply_annotated_song_play_counts, hydrate_song_play_counts
from .admin_permissions import (
    IsAdminPanelSession, IsAdminPanelUser, IsOwnerAdmin, bump_employee_session_version,
    employee_role, has_employee_permission, is_employee, is_employee_account, is_platform_admin,
    panel_identity, require_employee_permission, normalize_employee_permissions,
)
from .performance import CATALOG_VERSION_KEY, cache_increment
import os
import json
import logging
from PIL import Image
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError as DjangoValidationError

logger = logging.getLogger(__name__)

class AdminPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


_TAXONOMY_MODELS = {
    'genre': Genre,
    'subgenre': SubGenre,
    'mood': Mood,
    'tag': Tag,
}
_TAXONOMY_SHARED_KEYS = {
    'genre': 'genre_ids',
    'subgenre': 'sub_genre_ids',
    'mood': 'mood_ids',
    'tag': 'tag_ids',
}


def _taxonomy_model(kind):
    return _TAXONOMY_MODELS.get(str(kind or '').strip().lower())


def _taxonomy_queryset(kind):
    model = _taxonomy_model(kind)
    if model is None:
        return None
    annotations = {
        'admin_song_count': Count('songs', distinct=True),
    }
    if kind != 'tag':
        annotations['admin_album_count'] = Count('albums', distinct=True)
    else:
        annotations['admin_album_count'] = Value(0, output_field=IntegerField())
    if kind == 'genre':
        annotations['admin_child_count'] = Count('sub_genres', distinct=True)
    else:
        annotations['admin_child_count'] = Value(0, output_field=IntegerField())
    queryset = model.objects.annotate(**annotations).order_by('name', 'id')
    if kind == 'subgenre':
        queryset = queryset.select_related('parent_genre')
    return queryset


def _taxonomy_item_data(item, kind):
    parent = getattr(item, 'parent_genre', None) if kind == 'subgenre' else None
    songs = int(getattr(item, 'admin_song_count', 0) or 0)
    albums = int(getattr(item, 'admin_album_count', 0) or 0)
    children = int(getattr(item, 'admin_child_count', 0) or 0)
    return {
        'id': item.pk,
        'kind': kind,
        'name': item.name,
        'name_en': item.name_en,
        'slug': item.slug,
        'parent_genre': ({
            'id': parent.pk,
            'name': parent.name,
            'name_en': parent.name_en,
        } if parent is not None else None),
        'usage': {
            'songs': songs,
            'albums': albums,
            'child_subgenres': children,
            'direct_total': songs + albums,
        },
    }


def _taxonomy_release_workspace_impact(kind, item_id, child_subgenre_ids=()):
    """Return release workspaces whose JSON metadata would be affected.

    This intentionally scans the compact release metadata instead of relying on
    PostgreSQL-only JSON containment operators, keeping maintenance commands and
    tests portable to alternate database engines.
    """
    key = _TAXONOMY_SHARED_KEYS[kind]
    item_id = int(item_id)
    child_ids = {int(value) for value in child_subgenre_ids}
    affected = []
    direct = 0
    cascading = 0
    for release in ArtistRelease.objects.only('id', 'shared_metadata', 'status'):
        shared = release.shared_metadata if isinstance(release.shared_metadata, dict) else {}
        raw = shared.get(key) or []
        values = set()
        if isinstance(raw, (list, tuple)):
            for value in raw:
                try:
                    values.add(int(value))
                except (TypeError, ValueError):
                    continue
        hit_direct = item_id in values
        hit_child = False
        if child_ids:
            raw_children = shared.get('sub_genre_ids') or []
            child_values = set()
            if isinstance(raw_children, (list, tuple)):
                for value in raw_children:
                    try:
                        child_values.add(int(value))
                    except (TypeError, ValueError):
                        continue
            hit_child = bool(child_ids.intersection(child_values))
        if hit_direct or hit_child:
            affected.append(release.pk)
            direct += int(hit_direct)
            cascading += int(hit_child)
    return {
        'count': len(affected),
        'direct_count': direct,
        'cascade_count': cascading,
        'ids': affected,
    }


def _taxonomy_impact(item, kind, include_internal=False):
    song_count = item.songs.count()
    album_count = item.albums.count() if kind != 'tag' else 0
    child_ids = []
    child_count = 0
    cascade_song_count = 0
    cascade_album_count = 0
    if kind == 'genre':
        child_ids = list(item.sub_genres.values_list('id', flat=True))
        child_count = len(child_ids)
        if child_ids:
            cascade_song_count = Song.objects.filter(sub_genres__id__in=child_ids).distinct().count()
            cascade_album_count = Album.objects.filter(sub_genres__id__in=child_ids).distinct().count()
    release_impact = _taxonomy_release_workspace_impact(kind, item.pk, child_ids)
    total_song_count = song_count
    total_album_count = album_count
    if kind == 'genre' and child_ids:
        total_song_count = Song.objects.filter(
            Q(genres=item) | Q(sub_genres__id__in=child_ids)
        ).distinct().count()
        total_album_count = Album.objects.filter(
            Q(genres=item) | Q(sub_genres__id__in=child_ids)
        ).distinct().count()
    result = {
        'songs': song_count,
        'albums': album_count,
        'child_subgenres': child_count,
        'cascade_songs': cascade_song_count,
        'cascade_albums': cascade_album_count,
        'affected_songs': total_song_count,
        'affected_albums': total_album_count,
        'release_workspaces': release_impact['count'],
        'release_workspaces_direct': release_impact['direct_count'],
        'release_workspaces_cascade': release_impact['cascade_count'],
        'has_metadata_impact': bool(total_song_count or total_album_count or child_count or release_impact['count']),
    }
    if include_internal:
        result['_release_ids'] = release_impact['ids']
    return result


def _rewrite_id_list(values, source_id, replacement_id=None, remove_ids=()):
    if not isinstance(values, (list, tuple)):
        return values, False
    source_id = int(source_id)
    remove_ids = {int(value) for value in remove_ids}
    changed = False
    result = []
    seen = set()
    for raw in values:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = raw
        if value == source_id:
            changed = True
            if replacement_id is None:
                continue
            value = int(replacement_id)
        if isinstance(value, int) and value in remove_ids:
            changed = True
            continue
        marker = (type(value).__name__, str(value))
        if marker in seen:
            changed = True
            continue
        seen.add(marker)
        result.append(value)
    return result, changed


def _rewrite_release_workspace_taxonomy(kind, source_id, replacement_id=None, child_subgenre_ids=(), release_ids=None, replacement_parent_genre_id=None):
    now = timezone.now()
    changed_releases = []
    key = _TAXONOMY_SHARED_KEYS[kind]
    queryset = ArtistRelease.objects.all()
    if release_ids is not None:
        queryset = queryset.filter(pk__in=release_ids)
    for release in queryset.select_for_update().iterator(chunk_size=200):
        shared = dict(release.shared_metadata or {}) if isinstance(release.shared_metadata, dict) else {}
        values, changed = _rewrite_id_list(shared.get(key) or [], source_id, replacement_id)
        if changed:
            shared[key] = values
        if kind == 'subgenre' and replacement_id is not None and replacement_parent_genre_id is not None and changed:
            genre_values = list(shared.get('genre_ids') or []) if isinstance(shared.get('genre_ids') or [], (list, tuple)) else []
            normalized_genres = []
            seen_genres = set()
            for value in genre_values:
                try:
                    normalized = int(value)
                except (TypeError, ValueError):
                    normalized = value
                marker = (type(normalized).__name__, str(normalized))
                if marker not in seen_genres:
                    seen_genres.add(marker)
                    normalized_genres.append(normalized)
            if int(replacement_parent_genre_id) not in {value for value in normalized_genres if isinstance(value, int)}:
                normalized_genres.append(int(replacement_parent_genre_id))
                shared['genre_ids'] = normalized_genres
                changed = True

        if kind == 'genre' and child_subgenre_ids:
            child_ids = {int(value) for value in child_subgenre_ids}
            raw_children = shared.get('sub_genre_ids') or []
            selected_children = set()
            if isinstance(raw_children, (list, tuple)):
                for value in raw_children:
                    try:
                        selected_children.add(int(value))
                    except (TypeError, ValueError):
                        continue
            uses_reparented_child = bool(child_ids.intersection(selected_children))
            if replacement_id is None:
                child_values, child_changed = _rewrite_id_list(
                    raw_children, -1, None, remove_ids=child_subgenre_ids
                )
                if child_changed:
                    shared['sub_genre_ids'] = child_values
                    changed = True
            elif uses_reparented_child:
                genre_values = list(shared.get('genre_ids') or []) if isinstance(shared.get('genre_ids') or [], (list, tuple)) else []
                normalized_genres = []
                seen_genres = set()
                for value in genre_values:
                    try:
                        normalized = int(value)
                    except (TypeError, ValueError):
                        normalized = value
                    marker = (type(normalized).__name__, str(normalized))
                    if marker not in seen_genres:
                        seen_genres.add(marker)
                        normalized_genres.append(normalized)
                if int(replacement_id) not in {value for value in normalized_genres if isinstance(value, int)}:
                    normalized_genres.append(int(replacement_id))
                    shared['genre_ids'] = normalized_genres
                    changed = True
        if not changed:
            continue
        release.shared_metadata = shared
        release.validation_snapshot = {}
        release.lock_version = int(release.lock_version or 0) + 1
        release.updated_at = now
        changed_releases.append(release)
    if changed_releases:
        ArtistRelease.objects.bulk_update(
            changed_releases,
            ['shared_metadata', 'validation_snapshot', 'lock_version', 'updated_at'],
            batch_size=200,
        )
    return len(changed_releases)


def _add_ids_to_reverse_many_to_many(replacement, relation_name, ids, chunk_size=1000):
    replacement_manager = getattr(replacement, relation_name)
    batch = []
    processed = 0
    for pk in ids:
        batch.append(pk)
        if len(batch) >= chunk_size:
            replacement_manager.add(*batch)
            processed += len(batch)
            batch.clear()
    if batch:
        replacement_manager.add(*batch)
        processed += len(batch)
    return processed


def _transfer_reverse_many_to_many(source, replacement, relation_name, chunk_size=1000):
    source_manager = getattr(source, relation_name)
    return _add_ids_to_reverse_many_to_many(
        replacement, relation_name, source_manager.values_list('pk', flat=True).iterator(chunk_size=chunk_size), chunk_size
    )


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminTaxonomyListView(APIView):
    permission_classes = [IsAdminPanelUser]

    def get(self, request):
        items = {}
        summary = {}
        for kind in ('genre', 'subgenre', 'mood', 'tag'):
            rows = list(_taxonomy_queryset(kind))
            items[kind] = [_taxonomy_item_data(row, kind) for row in rows]
            summary[kind] = {
                'count': len(rows),
                'song_links': sum(int(getattr(row, 'admin_song_count', 0) or 0) for row in rows),
                'album_links': sum(int(getattr(row, 'admin_album_count', 0) or 0) for row in rows),
            }
        return Response({'items': items, 'summary': summary})

    def post(self, request):
        kind = str(request.data.get('kind') or '').strip().lower()
        model = _taxonomy_model(kind)
        if model is None:
            return Response({'kind': ['نوع دسته‌بندی معتبر نیست.']}, status=status.HTTP_400_BAD_REQUEST)
        serializer = AdminTaxonomySerializer(data=request.data, context={'kind': kind})
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                item = serializer.save()
                _bump_catalog_after_commit()
        except IntegrityError:
            return Response({'detail': 'موردی با همین نام یا شناسه URL هم‌زمان ثبت شده است.'}, status=status.HTTP_409_CONFLICT)
        row = _taxonomy_queryset(kind).get(pk=item.pk)
        return Response(_taxonomy_item_data(row, kind), status=status.HTTP_201_CREATED)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminTaxonomyDetailView(APIView):
    permission_classes = [IsAdminPanelUser]

    def _get(self, kind, pk):
        model = _taxonomy_model(kind)
        if model is None:
            return None
        return get_object_or_404(model, pk=pk)

    def get(self, request, kind, pk):
        kind = str(kind or '').strip().lower()
        item = self._get(kind, pk)
        if item is None:
            return Response({'detail': 'نوع دسته‌بندی معتبر نیست.'}, status=status.HTTP_404_NOT_FOUND)
        row = _taxonomy_queryset(kind).get(pk=item.pk)
        data = _taxonomy_item_data(row, kind)
        data['impact'] = _taxonomy_impact(item, kind)
        return Response(data)

    def patch(self, request, kind, pk):
        kind = str(kind or '').strip().lower()
        item = self._get(kind, pk)
        if item is None:
            return Response({'detail': 'نوع دسته‌بندی معتبر نیست.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = AdminTaxonomySerializer(item, data=request.data, partial=True, context={'kind': kind})
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                serializer.save()
                _bump_catalog_after_commit()
        except IntegrityError:
            return Response({'detail': 'موردی با همین نام یا شناسه URL هم‌زمان ثبت شده است.'}, status=status.HTTP_409_CONFLICT)
        row = _taxonomy_queryset(kind).get(pk=item.pk)
        return Response(_taxonomy_item_data(row, kind))

    def delete(self, request, kind, pk):
        kind = str(kind or '').strip().lower()
        model = _taxonomy_model(kind)
        if model is None:
            return Response({'detail': 'نوع دسته‌بندی معتبر نیست.'}, status=status.HTTP_404_NOT_FOUND)

        replacement_id = request.data.get('replacement_id')
        if replacement_id not in (None, ''):
            try:
                replacement_id = int(replacement_id)
            except (TypeError, ValueError):
                return Response({'replacement_id': ['جایگزین انتخاب‌شده معتبر نیست.']}, status=status.HTTP_400_BAD_REQUEST)
            if replacement_id == pk:
                return Response({'replacement_id': ['یک مورد نمی‌تواند جایگزین خودش باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        else:
            replacement_id = None

        with transaction.atomic():
            # Lock source/replacement in deterministic PK order. This prevents
            # opposite concurrent merges (A→B and B→A) from deadlocking.
            lock_ids = sorted({pk, *([replacement_id] if replacement_id is not None else [])})
            locked = {obj.pk: obj for obj in model.objects.select_for_update().filter(pk__in=lock_ids).order_by('pk')}
            item = locked.get(pk)
            if item is None:
                return Response({'detail': 'مورد موردنظر پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
            replacement = locked.get(replacement_id) if replacement_id is not None else None
            if replacement_id is not None and replacement is None:
                return Response({'replacement_id': ['مورد جایگزین پیدا نشد.']}, status=status.HTTP_404_NOT_FOUND)

            impact = _taxonomy_impact(item, kind, include_internal=True)
            release_ids = impact.pop('_release_ids', [])
            confirm_name = str(request.data.get('confirm_name') or '').strip()
            if impact['has_metadata_impact'] and confirm_name != item.name:
                return Response({'confirm_name': ['برای تأیید این حذف اثرگذار، نام فارسی را دقیق وارد کنید.']}, status=status.HTTP_400_BAD_REQUEST)

            acknowledged = request.data.get('allow_metadata_loss') is True
            if replacement is None and impact['has_metadata_impact'] and not acknowledged:
                return Response({
                    'detail': 'این مورد در متادیتا استفاده شده است. برای حذف بدون جایگزین باید آسیب متادیتا را صریحاً تأیید کنید.',
                    'impact': impact,
                    'requires_acknowledgement': True,
                }, status=status.HTTP_409_CONFLICT)

            child_ids = list(item.sub_genres.values_list('id', flat=True)) if kind == 'genre' else []
            moved_songs = 0
            moved_albums = 0
            reparented_subgenres = 0
            if replacement is not None:
                moved_songs = _transfer_reverse_many_to_many(item, replacement, 'songs')
                if kind != 'tag':
                    moved_albums = _transfer_reverse_many_to_many(item, replacement, 'albums')
                if kind == 'subgenre' and getattr(replacement, 'parent_genre_id', None):
                    # The source relation still exists until item.delete(), so stream
                    # ids directly instead of materializing large catalogs in memory.
                    parent = replacement.parent_genre
                    _add_ids_to_reverse_many_to_many(parent, 'songs', item.songs.values_list('pk', flat=True).iterator(chunk_size=1000))
                    _add_ids_to_reverse_many_to_many(parent, 'albums', item.albums.values_list('pk', flat=True).iterator(chunk_size=1000))
                if kind == 'genre' and child_ids:
                    # Content can legitimately carry a subgenre without the parent
                    # genre M2M.  Once children are re-parented, attach the new parent
                    # to that content as well so hierarchy remains coherent.
                    child_song_ids = Song.objects.filter(sub_genres__id__in=child_ids).values_list('pk', flat=True).distinct().iterator(chunk_size=1000)
                    child_album_ids = Album.objects.filter(sub_genres__id__in=child_ids).values_list('pk', flat=True).distinct().iterator(chunk_size=1000)
                    _add_ids_to_reverse_many_to_many(replacement, 'songs', child_song_ids)
                    _add_ids_to_reverse_many_to_many(replacement, 'albums', child_album_ids)
                    moved_songs = impact['affected_songs']
                    moved_albums = impact['affected_albums']
                    reparented_subgenres = SubGenre.objects.filter(parent_genre=item).update(parent_genre=replacement)

            rewritten_releases = _rewrite_release_workspace_taxonomy(
                kind,
                item.pk,
                replacement.pk if replacement is not None else None,
                child_subgenre_ids=child_ids,
                release_ids=release_ids,
                replacement_parent_genre_id=(replacement.parent_genre_id if kind == 'subgenre' and replacement is not None else None),
            )
            deleted_name = item.name
            item.delete()
            _bump_catalog_after_commit()

        return Response({
            'deleted': True,
            'kind': kind,
            'name': deleted_name,
            'replacement_id': replacement.pk if replacement is not None else None,
            'moved_songs': moved_songs,
            'moved_albums': moved_albums,
            'reparented_subgenres': reparented_subgenres,
            'rewritten_release_workspaces': rewritten_releases,
            'impact_before_delete': impact,
        })

def _admin_song_detail_queryset(include_drafts=False):
    queryset = Song.objects.select_related('artist', 'album')
    if not include_drafts:
        queryset = queryset.exclude(status=Song.STATUS_DRAFT)
    return (
        queryset
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

def _exclude_employee_accounts_for_employee(queryset, request):
    if is_employee(request.user):
        # Employee sessions must never receive peer employee accounts or owner/staff
        # accounts from the Users surface, even when probing filters manually.
        return queryset.exclude(
            Q(roles__contains=User.ROLE_MANAGER)
            | Q(roles__contains=User.ROLE_SUPERVISOR)
            | Q(roles__contains=User.ROLE_ADMIN)
            | Q(is_staff=True)
            | Q(is_superuser=True)
        )
    return queryset


def _visible_admin_user_or_404(request, pk):
    user = get_object_or_404(User.objects.select_related('artist_profile'), pk=pk)
    if is_employee(request.user):
        roles = set(user.roles or [])
        if is_employee_account(user) or user.is_staff or user.is_superuser or User.ROLE_ADMIN in roles:
            from django.http import Http404
            raise Http404
    return user


def _employee_queryset():
    return User.objects.filter(is_staff=False, is_superuser=False).filter(
        Q(roles=[User.ROLE_MANAGER]) | Q(roles=[User.ROLE_SUPERVISOR])
    )


def _revoke_employee_sessions(user):
    RefreshToken.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=timezone.now())


def _employee_can_edit_song_via_admin_release(user, song) -> bool:
    if not is_employee(user) or not has_employee_permission(user, 'release_add.edit'):
        return False
    return ArtistReleaseTrack.objects.filter(
        song=song,
        release__status=ArtistRelease.STATUS_DRAFT,
        release__status_history__from_status='',
        release__status_history__to_status=ArtistRelease.STATUS_DRAFT,
        release__status_history__note='پیش‌نویس انتشار توسط مدیر ایجاد شد.',
    ).exists()


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserListView(APIView):
    permission_classes = [IsAdminPanelUser]

    def get(self, request):
        role = str(request.query_params.get('role') or User.ROLE_AUDIENCE).strip()
        if role == 'employee':
            if not is_platform_admin(request.user):
                queryset = User.objects.none()
            else:
                queryset = _employee_queryset().select_related('artist_profile')
        else:
            queryset = User.objects.filter(roles__contains=role).select_related('artist_profile')
            queryset = _exclude_employee_accounts_for_employee(queryset, request)
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
        elif state == 'inactive':
            queryset = queryset.filter(is_active=False)
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
        return paginator.get_paginated_response(AdminUserSerializer(page, many=True, context={'request': request}).data)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserDetailView(APIView):
    permission_classes = [IsAdminPanelUser]

    @extend_schema(
        summary="جزئیات کاربر",
        description="دریافت اطلاعات کامل یک کاربر خاص بر اساس شناسه.",
        responses={200: AdminUserSerializer}
    )
    def get(self, request, pk):
        user = _visible_admin_user_or_404(request, pk)
        serializer = AdminUserSerializer(user, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کامل کاربر",
        description="ویرایش تمامی فیلدهای یک کاربر.",
        request=AdminUserSerializer,
        responses={200: AdminUserSerializer}
    )
    def put(self, request, pk):
        user = _visible_admin_user_or_404(request, pk)
        serializer = AdminUserSerializer(user, data=request.data, context={'request': request})
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
        user = _visible_admin_user_or_404(request, pk)
        serializer = AdminUserSerializer(user, data=request.data, partial=True, context={'request': request})
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
        user = _visible_admin_user_or_404(request, pk)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminUserBanView(APIView):
    """Soft, reversible account blocking. User content is never deleted here."""
    permission_classes = [IsAdminPanelUser]

    def post(self, request):
        user_id = request.data.get('user_id')
        banned = request.data.get('banned', True)
        if user_id in (None, ''):
            return Response({'detail': 'شناسه کاربر الزامی است.'}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(banned, str):
            banned = banned.strip().lower() in {'1', 'true', 'yes', 'on'}
        user = _visible_admin_user_or_404(request, user_id)
        if is_employee(request.user):
            required = 'artists.ban' if User.ROLE_ARTIST in set(user.roles or []) else 'users.ban'
            require_employee_permission(request.user, required)
        if user.pk == request.user.pk:
            return Response({'detail': 'امکان مسدود کردن حساب مدیر فعلی وجود ندارد.'}, status=status.HTTP_400_BAD_REQUEST)
        if user.is_staff:
            return Response({'detail': 'حساب مدیر از این بخش قابل مسدودسازی نیست.'}, status=status.HTTP_400_BAD_REQUEST)

        user.is_banned = bool(banned)
        user.is_active = not bool(banned)
        user.save(update_fields=['is_banned', 'is_active'])
        return Response({
            'message': 'کاربر با موفقیت مسدود شد.' if banned else 'مسدودی کاربر با موفقیت برداشته شد.',
            'user': AdminUserSerializer(user, context={'request': request}).data,
        })


def _admin_artist_upload(request, field_name, folder):
    upload = request.FILES.get(field_name)
    if not upload:
        return None
    max_size = 5 * 1024 * 1024 if field_name == 'profile_image_upload' else 10 * 1024 * 1024
    if getattr(upload, 'size', 0) > max_size:
        raise serializers.ValidationError({field_name: f'حجم تصویر باید حداکثر {max_size // (1024 * 1024)} مگابایت باشد.'})
    content_type = str(getattr(upload, 'content_type', '') or '').lower()
    extension = os.path.splitext(str(getattr(upload, 'name', '') or ''))[1].lower()
    if content_type not in {'image/jpeg', 'image/png', 'image/webp'} and extension not in {'.jpg', '.jpeg', '.png', '.webp'}:
        raise serializers.ValidationError({field_name: 'فرمت تصویر باید JPG، PNG یا WEBP باشد.'})
    try:
        upload.seek(0)
        with Image.open(upload) as image:
            image.verify()
        upload.seek(0)
        with Image.open(upload) as image:
            width, height = image.size
        upload.seek(0)
    except Exception:
        raise serializers.ValidationError({field_name: 'فایل تصویر قابل خواندن نیست.'})
    if field_name == 'profile_image_upload' and width != height:
        raise serializers.ValidationError({field_name: 'تصویر پروفایل باید مربعی باشد.'})
    url, _ = upload_file_to_r2(upload, folder=folder)
    return url


_ADMIN_ARTIST_SOCIALS = {
    'instagram': ('اینستاگرام', 'Instagram'),
    'twitter': ('توییتر', 'Twitter'),
    'youtube': ('یوتیوب', 'YouTube'),
    'telegram': ('تلگرام', 'Telegram'),
}


def _admin_artist_social_map(request):
    raw = request.data.get('social_accounts')
    if raw in (None, ''):
        return None
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(values, dict):
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError):
        raise serializers.ValidationError({'social_accounts': ['ساختار شبکه‌های اجتماعی معتبر نیست.']})
    validator = URLValidator(schemes=['http', 'https'])
    normalized = {}
    for raw_slug, raw_url in values.items():
        slug = str(raw_slug or '').strip().lower()
        if slug not in _ADMIN_ARTIST_SOCIALS:
            continue
        url = str(raw_url or '').strip()
        if url:
            try:
                validator(url)
            except DjangoValidationError:
                raise serializers.ValidationError({'social_accounts': [f'پیوند {_ADMIN_ARTIST_SOCIALS[slug][0]} معتبر نیست.']})
        normalized[slug] = url
    return normalized


def _save_admin_artist_socials(artist, values):
    if values is None:
        return
    for slug, url in values.items():
        name, name_en = _ADMIN_ARTIST_SOCIALS[slug]
        platform, _ = SocialPlatform.objects.get_or_create(slug=slug, defaults={'name': name, 'name_en': name_en})
        if not url:
            ArtistSocialAccount.objects.filter(artist=artist, platform=platform).delete()
        else:
            ArtistSocialAccount.objects.update_or_create(artist=artist, platform=platform, defaults={'url': url, 'username': ''})


def _admin_artist_payload(request):
    data = request.data.copy()
    # Upload fields are transport-only and are intentionally not serializer fields.
    for key in ('profile_image_upload', 'banner_image_upload', 'social_accounts'):
        if key in data:
            data.pop(key)
    if data.get('user') in ('', 'null', 'None'):
        data['user'] = None
    if data.get('date_of_birth') in ('', 'null', 'None'):
        data['date_of_birth'] = None
    if data.get('unique_id') in ('', 'null', 'None'):
        data['unique_id'] = None
    return data


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminArtistListView(APIView):
    permission_classes = [IsAdminPanelUser]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        queryset = Artist.objects.select_related('user').prefetch_related('social_account_links__platform').all()
        picker_only = is_employee(request.user) and not has_employee_permission(request.user, 'artists.view')
        query = str(request.query_params.get('q') or '').strip()
        if query:
            artist_query = (
                Q(name__icontains=query) | Q(artistic_name__icontains=query)
                | Q(name_en__icontains=query) | Q(artistic_name_en__icontains=query)
            )
            if not picker_only:
                artist_query |= Q(user__phone_number__icontains=query) | Q(email__icontains=query)
            queryset = queryset.filter(artist_query)
        verified = request.query_params.get('verified')
        if verified in {'true', 'false'}:
            queryset = queryset.filter(verified=verified == 'true')
        queryset = queryset.order_by('-created_at', '-id')
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        serialized = AdminArtistSerializer(page, many=True, context={'request': request}).data
        if picker_only:
            picker_fields = {'id', 'name', 'name_en', 'artistic_name', 'artistic_name_en', 'profile_image', 'verified'}
            serialized = [{key: row.get(key) for key in picker_fields} for row in serialized]
        return paginator.get_paginated_response(serialized)

    @extend_schema(summary="ایجاد هنرمند مستقل توسط مدیر", responses={201: AdminArtistSerializer})
    def post(self, request):
        uploaded = []
        try:
            data = _admin_artist_payload(request)
            if is_employee(request.user):
                employee_artist_fields = {
                    'name', 'name_en', 'artistic_name', 'artistic_name_en', 'unique_id',
                    'email', 'city', 'city_en', 'date_of_birth', 'address', 'address_en',
                    'id_number', 'bio', 'bio_en', 'verified',
                }
                forbidden = set(data.keys()) - employee_artist_fields
                if forbidden:
                    return Response(
                        {field: ['این فیلد از بخش مدیریت هنرمند قابل تغییر نیست.'] for field in forbidden},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            if is_employee(request.user) and any(str(data.get(field) or '').strip() for field in ('date_of_birth', 'address', 'address_en', 'id_number')):
                require_employee_permission(request.user, 'artists.kyc')
            social_map = _admin_artist_social_map(request)
            profile_url = _admin_artist_upload(request, 'profile_image_upload', 'artists/profile')
            if profile_url:
                data['profile_image'] = profile_url
                uploaded.append(profile_url)
            banner_url = _admin_artist_upload(request, 'banner_image_upload', 'artists/banner')
            if banner_url:
                data['banner_image'] = banner_url
                uploaded.append(banner_url)
            if is_employee(request.user) and bool(data.get('verified')):
                require_employee_permission(request.user, 'artists.verify')
            serializer = AdminArtistSerializer(data=data, context={'request': request})
            serializer.is_valid(raise_exception=True)
            with transaction.atomic():
                artist = serializer.save()
                _save_admin_artist_socials(artist, social_map)
            return Response(AdminArtistSerializer(artist, context={'request': request}).data, status=status.HTTP_201_CREATED)
        except serializers.ValidationError as exc:
            cleanup_r2_urls(uploaded)
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            cleanup_r2_urls(uploaded)
            return Response({'detail': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminArtistDetailView(APIView):
    permission_classes = [IsAdminPanelUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات هنرمند",
        description="دریافت اطلاعات کامل یک هنرمند خاص.",
        responses={200: AdminArtistSerializer}
    )
    def get(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        serializer = AdminArtistSerializer(artist, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کامل هنرمند",
        description="ویرایش تمامی اطلاعات یک هنرمند.",
        request=AdminArtistSerializer,
        responses={200: AdminArtistSerializer}
    )
    def put(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        return self._update_artist(request, artist, partial=False)

    @extend_schema(
        summary="ویرایش جزئی هنرمند",
        description="ویرایش برخی از اطلاعات یک هنرمند.",
        request=AdminArtistSerializer,
        responses={200: AdminArtistSerializer}
    )
    def patch(self, request, pk):
        artist = get_object_or_404(Artist, pk=pk)
        return self._update_artist(request, artist, partial=True)

    def _update_artist(self, request, artist, partial):
        uploaded = []
        old_media = []
        try:
            data = _admin_artist_payload(request)
            if is_employee(request.user):
                employee_artist_fields = {
                    'name', 'name_en', 'artistic_name', 'artistic_name_en', 'unique_id',
                    'email', 'city', 'city_en', 'date_of_birth', 'address', 'address_en',
                    'id_number', 'bio', 'bio_en', 'verified',
                }
                forbidden = set(data.keys()) - employee_artist_fields
                if forbidden:
                    return Response(
                        {field: ['این فیلد از بخش مدیریت هنرمند قابل تغییر نیست.'] for field in forbidden},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            if is_employee(request.user) and any(field in data for field in ('date_of_birth', 'address', 'address_en', 'id_number')):
                require_employee_permission(request.user, 'artists.kyc')
            if is_employee(request.user) and 'verified' in data and bool(data.get('verified')) != bool(artist.verified):
                require_employee_permission(request.user, 'artists.verify')
            social_map = _admin_artist_social_map(request)
            profile_url = _admin_artist_upload(request, 'profile_image_upload', 'artists/profile')
            if profile_url:
                data['profile_image'] = profile_url
                uploaded.append(profile_url)
                if artist.profile_image:
                    old_media.append(artist.profile_image)
            banner_url = _admin_artist_upload(request, 'banner_image_upload', 'artists/banner')
            if banner_url:
                data['banner_image'] = banner_url
                uploaded.append(banner_url)
                if artist.banner_image:
                    old_media.append(artist.banner_image)
            serializer = AdminArtistSerializer(artist, data=data, partial=partial, context={'request': request})
            serializer.is_valid(raise_exception=True)
            with transaction.atomic():
                artist = serializer.save()
                _save_admin_artist_socials(artist, social_map)
            cleanup_r2_urls([value for value in old_media if value not in {artist.profile_image, artist.banner_image}])
            return Response(AdminArtistSerializer(artist, context={'request': request}).data)
        except serializers.ValidationError as exc:
            cleanup_r2_urls(uploaded)
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            cleanup_r2_urls(uploaded)
            return Response({'detail': str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

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
        media_urls = [value for value in (artist.profile_image, artist.banner_image) if value]
        with transaction.atomic():
            artist.delete()
            if media_urls:
                transaction.on_commit(lambda values=tuple(media_urls): cleanup_r2_urls(values))
        return Response(status=status.HTTP_204_NO_CONTENT)

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPendingArtistListView(APIView):
    permission_classes = [IsAdminPanelUser]

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
        serializer = AdminArtistAuthSerializer(result_page, many=True, context={'request': request})
        return paginator.get_paginated_response(serializer.data)

@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPendingArtistDetailView(APIView):
    permission_classes = [IsAdminPanelUser]

    @extend_schema(
        summary="جزئیات درخواست هنرمند",
        description="دریافت جزئیات یک درخواست خاص برای بررسی.",
        responses={200: AdminArtistAuthSerializer}
    )
    def get(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth, context={'request': request})
        return Response(serializer.data)

    @extend_schema(
        summary="ویرایش کامل درخواست",
        description="ویرایش تمامی اطلاعات یک درخواست عضویت.",
        request=AdminArtistAuthSerializer,
        responses={200: AdminArtistAuthSerializer}
    )
    def put(self, request, pk):
        auth = get_object_or_404(ArtistAuth, pk=pk)
        serializer = AdminArtistAuthSerializer(auth, data=request.data, context={'request': request})
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
        if is_employee(request.user):
            submitted = set(request.data.keys())
            if not submitted or submitted - {'status', 'is_verified'}:
                return Response({'detail': 'فقط نتیجه بررسی درخواست قابل ثبت است.'}, status=status.HTTP_400_BAD_REQUEST)
            requested_status = str(request.data.get('status') or '').strip()
            if requested_status not in {ArtistAuth.STATUS_ACCEPTED, ArtistAuth.STATUS_REJECTED}:
                return Response({'status': ['نتیجه بررسی باید تأیید یا رد باشد.']}, status=status.HTTP_400_BAD_REQUEST)
            expected_verified = requested_status == ArtistAuth.STATUS_ACCEPTED
            raw_verified = request.data.get('is_verified', expected_verified)
            if isinstance(raw_verified, str):
                raw_verified = raw_verified.strip().lower() in {'1', 'true', 'yes', 'on'}
            if bool(raw_verified) != expected_verified:
                return Response({'is_verified': ['وضعیت تأیید با نتیجه بررسی هماهنگ نیست.']}, status=status.HTTP_400_BAD_REQUEST)
        serializer = AdminArtistAuthSerializer(auth, data=request.data, partial=True, context={'request': request})
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
    permission_classes = [IsOwnerAdmin]

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
    permission_classes = [IsAdminPanelUser]

    def get(self, request):
        typ = str(request.query_params.get('type') or 'audience').strip()
        query = str(request.query_params.get('q') or '').strip()
        paginator = AdminPagination()
        if typ == 'audience':
            qs = User.objects.filter(roles__contains=User.ROLE_AUDIENCE)
            qs = _exclude_employee_accounts_for_employee(qs, request)
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
        return paginator.get_paginated_response(serializer_cls(page, many=True, context={'request': request}).data)



@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminSongListView(APIView):
    """List songs for admin with status filtering."""
    permission_classes = [IsAdminPanelUser]
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

        picker_only = is_employee(request.user) and not (
            has_employee_permission(request.user, 'songs.view')
            or has_employee_permission(request.user, 'release_add.view')
        )
        if picker_only:
            status_filter = Song.STATUS_PUBLISHED
        include_drafts = False if picker_only else str(request.query_params.get('include_drafts') or '').lower() in {'1', 'true', 'yes'}
        base_songs = Song.objects.all() if include_drafts else Song.objects.exclude(status=Song.STATUS_DRAFT)
        songs = (
            base_songs
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
        artist_id = str(request.query_params.get('artist_id') or '').strip()
        if artist_id:
            try:
                artist_id_value = int(artist_id)
            except (TypeError, ValueError):
                return Response({'artist_id': ['شناسه هنرمند معتبر نیست.']}, status=status.HTTP_400_BAD_REQUEST)
            if artist_id_value <= 0:
                return Response({'artist_id': ['شناسه هنرمند معتبر نیست.']}, status=status.HTTP_400_BAD_REQUEST)
            songs = songs.filter(artist_id=artist_id_value)
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
        serialized = AdminSongSerializer(page, many=True).data
        if picker_only:
            picker_fields = {'id', 'title', 'title_en', 'artist', 'artist_name', 'cover_image', 'status'}
            serialized = [{key: row.get(key) for key in picker_fields} for row in serialized]
        return paginator.get_paginated_response(serialized)


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
    permission_classes = [IsAdminPanelUser]
    parser_classes = [MultiPartParser, FormParser]

    @extend_schema(
        summary="جزئیات آهنگ",
        description="دریافت اطلاعات کامل یک آهنگ خاص.",
        responses={200: AdminSongSerializer}
    )
    def get(self, request, pk):
        song = get_object_or_404(_admin_song_detail_queryset(include_drafts=True), pk=pk)
        if is_employee(request.user) and not has_employee_permission(request.user, 'songs.view'):
            if not _employee_can_edit_song_via_admin_release(request.user, song):
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied('این آهنگ در پیش‌نویس انتشار مدیریتی قابل مشاهده نیست.')
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
        song = get_object_or_404(Song.objects.all(), pk=pk)
        if is_employee(request.user) and not has_employee_permission(request.user, 'songs.edit'):
            if not _employee_can_edit_song_via_admin_release(request.user, song):
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied('این آهنگ در پیش‌نویس انتشار مدیریتی قابل ویرایش نیست.')
        return self._update_song(request, song, partial=True)

    @extend_schema(
        summary="ویرایش کامل آهنگ",
        description="ویرایش تمامی فیلدهای آهنگ.",
        request=AdminSongSerializer,
        responses={200: AdminSongSerializer}
    )
    def put(self, request, pk):
        song = get_object_or_404(Song.objects.all(), pk=pk)
        if is_employee(request.user) and not has_employee_permission(request.user, 'songs.edit'):
            if not _employee_can_edit_song_via_admin_release(request.user, song):
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied('این آهنگ در پیش‌نویس انتشار مدیریتی قابل ویرایش نیست.')
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
                Song.objects.select_for_update(), pk=pk
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
        submitted_fields = set(request.data.keys())
        if is_employee(request.user):
            # Keep employee song editing at the metadata/media surface exposed by the
            # product. Workflow state, ownership and storage URLs remain server/owner
            # controlled even if a custom request is crafted manually.
            employee_editable_fields = {
                'title', 'title_en', 'featured_artist_ids', 'is_single',
                'album_disc_number', 'album_track_number', 'cover_image_upload',
                'release_date', 'language', 'genres', 'sub_genres', 'moods', 'tags',
                'description', 'description_en', 'lyrics', 'lyrics_en', 'tempo',
                'energy', 'danceability', 'valence', 'acousticness',
                'instrumentalness', 'live_performed', 'speechiness', 'label',
                'label_en', 'producers', 'producers_en', 'composers', 'composers_en',
                'lyricists', 'lyricists_en', 'credits', 'credits_en',
            }
            forbidden = submitted_fields - employee_editable_fields
            if forbidden:
                return Response(
                    {field: ['این فیلد از بخش ویرایش متادیتا قابل تغییر نیست.'] for field in forbidden},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        data = request.data.copy()
        
        # Normalize repeated multipart list inputs, including explicit clears.
        list_fields = [
            'featured_artist_ids', 'producers', 'producers_en', 'composers', 'composers_en',
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
            # A cover uploaded directly for a track must remain track-owned.
            # Otherwise the next release-level sync would treat an inherited
            # collection cover as authoritative and overwrite this admin crop.
            if cover_image:
                for link in ArtistReleaseTrack.objects.filter(song=song):
                    extras = dict(link.extras or {})
                    if extras.get('_cover_source') != 'track':
                        extras['_cover_source'] = 'track'
                        ArtistReleaseTrack.objects.filter(pk=link.pk).update(extras=extras, updated_at=timezone.now())
            updated_song = get_object_or_404(_admin_song_detail_queryset(include_drafts=True), pk=song.pk)
            hydrate_song_play_counts([updated_song])
            return Response(AdminSongSerializer(updated_song).data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminReportListView(APIView):
    """List reports for admin with filtering."""
    permission_classes = [IsAdminPanelUser]

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
        qs = Report.objects.select_related('user', 'song', 'artist', 'reported_user').all().order_by('-created_at')
        
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
    permission_classes = [IsAdminPanelUser]

    @extend_schema(
        summary="جزئیات گزارش",
        description="دریافت اطلاعات کامل یک گزارش خاص.",
        responses={200: AdminReportSerializer}
    )
    def get(self, request, pk):
        report = get_object_or_404(Report.objects.select_related('user', 'song', 'artist', 'reported_user'), pk=pk)
        serializer = AdminReportSerializer(report)
        return Response(serializer.data)

    @extend_schema(
        summary="بروزرسانی گزارش",
        description="تغییر وضعیت بررسی گزارش.",
        request=AdminReportSerializer,
        responses={200: AdminReportSerializer}
    )
    def put(self, request, pk):
        report = get_object_or_404(Report.objects.select_related('user', 'song', 'artist', 'reported_user'), pk=pk)
        if is_employee(request.user):
            if set(request.data.keys()) - {'has_reviewed'}:
                return Response({'detail': 'فقط وضعیت بررسی گزارش قابل تغییر است.'}, status=status.HTTP_400_BAD_REQUEST)
            reviewed_value = request.data.get('has_reviewed')
            if reviewed_value not in {True, 'true', 'True', '1', 1}:
                return Response({'detail': 'گزارش فقط می‌تواند به‌عنوان بررسی‌شده ثبت شود.'}, status=status.HTTP_400_BAD_REQUEST)
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
        report = get_object_or_404(Report.objects.select_related('user', 'song', 'artist', 'reported_user'), pk=pk)
        report.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminPlayConfigurationView(APIView):
    """View for admin to manage global play and price settings."""
    permission_classes = [IsAdminPanelUser]

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
    permission_classes = [IsAdminPanelUser]
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
    permission_classes = [IsAdminPanelUser]
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
    permission_classes = [IsAdminPanelUser]
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
    permission_classes = [IsAdminPanelUser]
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
    permission_classes = [IsAdminPanelUser]

    @extend_schema(
        summary="لیست آلبوم‌ها",
        description="دریافت لیست تمامی آلبوم‌ها (به جز تک‌آهنگ‌ها) با قابلیت صفحه‌بندی.",
        responses={200: AdminAlbumSerializer(many=True)}
    )
    def get(self, request):
        picker_only = is_employee(request.user) and not has_employee_permission(request.user, 'albums.view')
        visible_song_filter = Q(songs__status=Song.STATUS_PUBLISHED) if picker_only else ~Q(songs__status=Song.STATUS_DRAFT)
        visible_song_queryset = (
            _admin_song_detail_queryset().filter(status=Song.STATUS_PUBLISHED)
            if picker_only else _admin_song_detail_queryset()
        )
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
            Prefetch('songs', queryset=visible_song_queryset, to_attr='_admin_visible_songs')
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
        serialized = AdminAlbumSerializer(page, many=True).data
        if picker_only:
            picker_fields = {'id', 'title', 'title_en', 'artist', 'artist_name', 'cover_image', 'release_date'}
            serialized = [{key: row.get(key) for key in picker_fields} for row in serialized]
        return paginator.get_paginated_response(serialized)



@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminAlbumDetailView(APIView):
    """Retrieve, update or delete an album for admin."""
    permission_classes = [IsAdminPanelUser]
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
        if is_employee(request.user):
            editable = {'title', 'title_en', 'release_date', 'description', 'description_en'}
            forbidden = set(request.data.keys()) - editable
            if forbidden:
                return Response(
                    {field: ['این فیلد از بخش ویرایش آلبوم قابل تغییر نیست.'] for field in forbidden},
                    status=status.HTTP_400_BAD_REQUEST,
                )
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
    permission_classes = [IsAdminPanelUser]

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
    permission_classes = [IsAdminPanelUser]

    @staticmethod
    def _total(qs, field='amount'):
        return float(qs.aggregate(total=Sum(field))['total'] or 0)

    def get(self, request):
        show_payments = is_platform_admin(request.user) or has_employee_permission(request.user, 'finance.payments')
        show_payouts = is_platform_admin(request.user) or has_employee_permission(request.user, 'finance.payouts')
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        last_7 = now - timedelta(days=7)
        last_30 = now - timedelta(days=30)

        def period(start_date, end_date=None):
            data = {}
            if show_payments:
                payments = PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_SUCCESS, created_at__gte=start_date)
                if end_date:
                    payments = payments.filter(created_at__lte=end_date)
                total = self._total(payments)
                data.update({'revenue': total, 'successful_payment_count': payments.count(), 'total_payments': total, 'count_payments': payments.count()})
            if show_payouts:
                done = DepositRequest.objects.filter(status=DepositRequest.STATUS_DONE, submission_date__gte=start_date)
                opened = DepositRequest.objects.filter(status__in=[DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED], submission_date__gte=start_date)
                if end_date:
                    done = done.filter(submission_date__lte=end_date)
                    opened = opened.filter(submission_date__lte=end_date)
                total = self._total(done)
                data.update({'paid_to_artists': total, 'paid_to_artists_count': done.count(), 'pending_artist_payouts': self._total(opened), 'pending_artist_payout_count': opened.count(), 'total_deposits': total, 'count_deposits': done.count()})
            return data

        all_start = timezone.make_aware(timezone.datetime(2000, 1, 1))
        result = {'today': period(today_start), 'last_7_days': period(last_7), 'last_30_days': period(last_30), 'all_time': period(all_start)}
        if show_payments:
            result['payment_status'] = {
                'pending': PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_PENDING).count(),
                'success': PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_SUCCESS).count(),
                'failed': PaymentTransaction.objects.filter(status=PaymentTransaction.STATUS_FAILED).count(),
            }
        if show_payouts:
            result['payout_status'] = {value: DepositRequest.objects.filter(status=value).count() for value in [DepositRequest.STATUS_PENDING, DepositRequest.STATUS_APPROVED, DepositRequest.STATUS_REJECTED, DepositRequest.STATUS_DONE]}
        start_param = request.query_params.get('start')
        end_param = request.query_params.get('end')
        if start_param and end_param:
            try:
                start_dt = timezone.datetime.fromisoformat(start_param); end_dt = timezone.datetime.fromisoformat(end_param)
                if timezone.is_naive(start_dt): start_dt = timezone.make_aware(start_dt)
                if timezone.is_naive(end_dt): end_dt = timezone.make_aware(end_dt)
                if len(end_param) == 10: end_dt = end_dt.replace(hour=23, minute=59, second=59)
                result['custom_period'] = period(start_dt, end_dt)
            except (TypeError, ValueError):
                result['custom_period_error'] = 'فرمت تاریخ معتبر نیست.'
        return Response(result)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminArtistEarningsListView(APIView):
    permission_classes = [IsAdminPanelUser]

    def get(self, request):
        queryset = Artist.objects.select_related('user').annotate(
            stream_count=Count('songs__play_counts', distinct=True),
            # PostgreSQL puts NULLs first for DESC ordering. Without a database-level
            # zero default, artists with no plays could appear ahead of artists with
            # real earnings when sorting by "most income".
            earned_total=Sum('songs__play_counts__pay', default=Decimal('0.000000')),
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
    permission_classes = [IsAdminPanelUser]

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
    permission_classes = [IsAdminPanelUser]

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
    permission_classes = [IsAdminPanelUser]
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
        if is_employee(request.user):
            direct_relations = {'songs', 'albums', 'playlists'} & set(request.data.keys())
            if direct_relations:
                return Response(
                    {field: ['محتوای بخش باید از انتخاب‌گر همین صفحه ثبت شود.'] for field in direct_relations},
                    status=status.HTTP_400_BAD_REQUEST,
                )
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
    permission_classes = [IsAdminPanelUser]
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
        if is_employee(request.user):
            direct_relations = {'songs', 'albums', 'playlists'} & set(request.data.keys())
            if direct_relations:
                return Response(
                    {field: ['محتوای بخش باید از انتخاب‌گر همین صفحه ثبت شود.'] for field in direct_relations},
                    status=status.HTTP_400_BAD_REQUEST,
                )
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
    permission_classes = [IsAdminPanelUser]
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
    permission_classes = [IsAdminPanelUser]
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
    permission_classes = [IsAdminPanelUser]

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
    permission_classes = [IsAdminPanelUser]
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
    permission_classes = [IsAdminPanelUser]
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
class AdminPanelSessionView(APIView):
    """Authoritative custom-panel identity and employee permissions."""
    permission_classes = [IsAdminPanelSession]

    def get(self, request):
        return Response(panel_identity(request.user))


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEmployeeListView(APIView):
    """Owner-only employee directory and creation endpoint."""
    permission_classes = [IsOwnerAdmin]

    def get(self, request):
        queryset = _employee_queryset().order_by('-date_joined', '-id')
        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(
                Q(phone_number__icontains=query) | Q(first_name__icontains=query)
                | Q(last_name__icontains=query) | Q(email__icontains=query)
            )
        role = str(request.query_params.get('role') or '').strip()
        if role in {User.ROLE_MANAGER, User.ROLE_SUPERVISOR}:
            queryset = queryset.filter(roles=[role])
        state = str(request.query_params.get('state') or '').strip()
        if state == 'active':
            queryset = queryset.filter(is_active=True)
        elif state == 'inactive':
            queryset = queryset.filter(is_active=False)
        paginator = AdminPagination()
        page = paginator.paginate_queryset(queryset, request)
        return paginator.get_paginated_response(AdminEmployeeSerializer(page, many=True).data)

    def post(self, request):
        serializer = AdminEmployeeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        employee = serializer.save()
        logger.info('Admin employee created actor=%s employee=%s role=%s', request.user.pk, employee.pk, employee_role(employee))
        return Response(AdminEmployeeSerializer(employee).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEmployeeDetailView(APIView):
    """Owner-only employee read/update/delete endpoint."""
    permission_classes = [IsOwnerAdmin]

    def _get(self, pk):
        return get_object_or_404(_employee_queryset(), pk=pk)

    def get(self, request, pk):
        return Response(AdminEmployeeSerializer(self._get(pk)).data)

    def patch(self, request, pk):
        employee = self._get(pk)
        before_permissions = normalize_employee_permissions(employee.permissions)
        before_active = employee.is_active
        before_role = employee_role(employee)
        serializer = AdminEmployeeSerializer(employee, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        employee = serializer.save()
        access_changed = (
            before_permissions != normalize_employee_permissions(employee.permissions)
            or before_active != employee.is_active
            or before_role != employee_role(employee)
        )
        if access_changed:
            bump_employee_session_version(employee)
            _revoke_employee_sessions(employee)
        logger.info('Admin employee updated actor=%s employee=%s access_changed=%s', request.user.pk, employee.pk, access_changed)
        return Response(AdminEmployeeSerializer(employee).data)

    def delete(self, request, pk):
        employee = self._get(pk)
        employee_id = employee.pk
        _revoke_employee_sessions(employee)
        employee.delete()
        logger.info('Admin employee deleted actor=%s employee=%s', request.user.pk, employee_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminEmployeePasswordView(APIView):
    """Owner-only secure reset; existing password hashes are never reversible."""
    permission_classes = [IsOwnerAdmin]

    def post(self, request, pk):
        employee = get_object_or_404(_employee_queryset(), pk=pk)
        password = request.data.get('password')
        if not isinstance(password, str) or len(password) < 8:
            return Response({'password': ['رمز عبور باید حداقل ۸ کاراکتر باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        if len(password) > 128:
            return Response({'password': ['رمز عبور بیش از حد طولانی است.']}, status=status.HTTP_400_BAD_REQUEST)
        if employee.check_password(password):
            return Response({'password': ['رمز جدید باید با رمز فعلی متفاوت باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        employee.set_password(password)
        employee.failed_login_attempts = 0
        employee.locked_until = None
        bump_employee_session_version(employee, save=False)
        employee.save(update_fields=['password', 'failed_login_attempts', 'locked_until', 'permissions'])
        _revoke_employee_sessions(employee)
        logger.info('Admin employee password reset actor=%s employee=%s', request.user.pk, employee.pk)
        return Response({'detail': 'رمز عبور کارمند تغییر کرد و نشست‌های قبلی او بسته شد.'})


@extend_schema(tags=['Admin App Endpoints اندپوینت های اپلیکیشن ادمین'])
class AdminDepositRequestDetailView(APIView):
    permission_classes = [IsAdminPanelUser]

    def get(self, request, pk):
        deposit = get_object_or_404(DepositRequest.objects.select_related('artist'), pk=pk)
        return Response(AdminDepositRequestSerializer(deposit).data)

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
    permission_classes = [IsOwnerAdmin]

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
    permission_classes = [IsAdminPanelUser]

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
    permission_classes = [IsAdminPanelUser]

    def get(self, request, pk):
        ticket = get_object_or_404(SupportTicket.objects.select_related('user', 'responded_by'), pk=pk)
        return Response(AdminSupportTicketSerializer(ticket).data)

    def patch(self, request, pk):
        ticket = get_object_or_404(SupportTicket, pk=pk)
        if is_employee(request.user):
            forbidden = set(request.data.keys()) - {'status', 'admin_response'}
            if forbidden:
                return Response(
                    {field: ['این فیلد از بخش رسیدگی به تیکت قابل تغییر نیست.'] for field in forbidden},
                    status=status.HTTP_400_BAD_REQUEST,
                )
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
    permission_classes = [IsAdminPanelUser]

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
    permission_classes = [IsAdminPanelUser]

    def get(self, request, pk):
        promotion = get_object_or_404(
            SongPromotion.objects.select_related('song', 'song__artist', 'created_by'), pk=pk
        )
        return Response(AdminSongPromotionSerializer(promotion).data)

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
