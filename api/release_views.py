from __future__ import annotations

import json
import logging
import os
from PIL import Image
from django.db import transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .admin_permissions import IsAdminPanelUser, has_employee_permission, is_employee, require_employee_permission
from .models import (
    Album,
    Artist,
    ArtistRelease,
    ArtistReleaseStatusHistory,
    ArtistReleaseTrack,
    ReleaseContributor,
    Song,
    PlayCount,
    User,
)
from .release_service import (
    apply_track_metadata,
    approve_release,
    change_status,
    create_revision,
    ensure_editable_song,
    mark_release_for_review,
    materialize_release,
    merged_release_metadata,
    merged_shared,
    normalize_track_extras,
    prepare_release,
    publish_due_releases,
    release_queryset,
    release_removal_state,
    scheduled_datetime,
    serialize_release,
    snapshot_song,
    sync_release_artwork,
    sync_release_tracks,
    take_down_release,
    validation_payload,
)
from .utils import MediaPipelineError, cleanup_r2_urls, upload_file_to_r2, convert_to_128kbps, get_audio_info, make_safe_filename

logger = logging.getLogger(__name__)


ADMIN_CREATED_HISTORY_NOTE = 'پیش‌نویس انتشار توسط مدیر ایجاد شد.'


def _admin_created_release(release: ArtistRelease) -> bool:
    # No schema change is needed: the baseline already records an immutable first
    # status-history row for each workflow. This survives creator deactivation/deletion.
    return release.status_history.filter(
        from_status='',
        to_status=ArtistRelease.STATUS_DRAFT,
        note=ADMIN_CREATED_HISTORY_NOTE,
    ).exists()


def _require_employee_release_read(user, release: ArtistRelease) -> None:
    if not is_employee(user):
        return
    admin_created = _admin_created_release(release)
    if release.status == ArtistRelease.STATUS_DRAFT:
        if admin_created and has_employee_permission(user, 'release_add.view'):
            return
    elif has_employee_permission(user, 'releases.view'):
        return
    elif admin_created and has_employee_permission(user, 'release_add.view'):
        return
    from rest_framework.exceptions import PermissionDenied
    raise PermissionDenied('شما اجازه مشاهده این انتشار را ندارید.')


def _require_employee_admin_release(user, release: ArtistRelease, permission: str = 'release_add.edit') -> None:
    if not is_employee(user):
        return
    require_employee_permission(user, permission)
    if not _admin_created_release(release):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied('این انتشار متعلق به جریان افزودن انتشار مدیریت نیست.')


def _artist_for_user(user):
    if not user or not user.is_authenticated or User.ROLE_ARTIST not in (user.roles or []):
        return None
    try:
        return user.artist_profile
    except Artist.DoesNotExist:
        return None


def _id_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            value = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            value = value.replace('،', ',').split(',')
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    result = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in result:
            result.append(number)
    return result



def _lock_version_error(release, data):
    if 'lock_version' not in data:
        return None
    try:
        expected = int(data.get('lock_version'))
    except (TypeError, ValueError):
        return Response({'lock_version': ['نسخه معتبر ویرایش را ارسال کنید.']}, status=status.HTTP_400_BAD_REQUEST)
    if expected != release.lock_version:
        return Response({
            'detail': 'این انتشار در تب یا نشست دیگری تغییر کرده است. پیش از ذخیره دوباره، صفحه را به‌روزرسانی کنید.',
            'code': 'release_version_conflict',
            'current_lock_version': release.lock_version,
        }, status=status.HTTP_409_CONFLICT)
    return None

def _touch_release(release: ArtistRelease, *, clear_validation: bool = True) -> None:
    update_fields = ['lock_version', 'updated_at']
    release.lock_version += 1
    if clear_validation:
        release.validation_snapshot = {}
        update_fields.append('validation_snapshot')
    release.save(update_fields=update_fields)


def _draft_or_409(release):
    if release.status not in {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW}:
        return Response(
            {
                'detail': 'برای بازگرداندن این انتشار و ترک‌های مرتبط به صف بررسی، ویرایش را تأیید کنید.',
                'code': 'release_reapproval_required',
            },
            status=status.HTTP_409_CONFLICT,
        )
    return None


def _confirmed_reapproval(data) -> bool:
    value = data.get('confirm_re_review') if hasattr(data, 'get') else None
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _cleanup_unreferenced_release_media(urls):
    """Remove release media only after every database reference is gone."""
    for url in dict.fromkeys(value for value in urls if value):
        if Song.objects.filter(
            Q(audio_file=url) |
            Q(converted_audio_url=url) |
            Q(preview_audio_url=url) |
            Q(cover_image=url)
        ).exists():
            continue
        if Album.objects.filter(cover_image=url).exists():
            continue
        if ArtistRelease.objects.filter(release_metadata__cover_url=url).exists():
            continue
        cleanup_r2_urls([url])


def _set_release_taken_down(release, actor, note):
    if release.status == ArtistRelease.STATUS_TAKEN_DOWN:
        return
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
        note=note,
        actor=actor,
    )


def _renumber_release_links(release):
    links = list(
        ArtistReleaseTrack.objects.select_for_update()
        .filter(release=release)
        .order_by('position', 'id')
    )
    for index, link in enumerate(links, start=1):
        if link.position != index:
            ArtistReleaseTrack.objects.filter(pk=link.pk).update(position=1000 + index)
    for index, link in enumerate(links, start=1):
        ArtistReleaseTrack.objects.filter(pk=link.pk).update(position=index)


def _legacy_track(song):
    return {
        'id': song.id,
        'title': song.title,
        'title_en': song.title_en,
        'artist_name': song.artist.name if song.artist_id else '',
        'album_title': song.album.title if song.album_id else None,
        'duration_display': song.duration_display,
        'duration_seconds': song.duration_seconds,
        'original_format': song.original_format,
        'cover_image': song.cover_image,
        'audio_file': song.audio_file,
        'status': song.status,
        'has_audio': bool(song.audio_file),
        'metadata_completion': 100,
        'missing_metadata': [],
        'release_extras': {},
    }


def _legacy_release(album, request=None):
    songs = list(album.songs.all())
    statuses = {song.status for song in songs}
    if songs and statuses == {Song.STATUS_DELETED}:
        release_status = ArtistRelease.STATUS_TAKEN_DOWN
    elif Song.STATUS_PUBLISHED in statuses:
        release_status = ArtistRelease.STATUS_LIVE
    elif Song.STATUS_PENDING in statuses:
        release_status = ArtistRelease.STATUS_IN_REVIEW
    elif Song.STATUS_REJECTED in statuses:
        release_status = ArtistRelease.STATUS_REJECTED
    elif Song.STATUS_APPROVED in statuses:
        release_status = ArtistRelease.STATUS_APPROVED
    else:
        release_status = ArtistRelease.STATUS_DRAFT
    return {
        'id': f'legacy-album-{album.id}',
        'legacy': True,
        'legacy_kind': 'album',
        'album_id': album.id,
        'title': album.title,
        'title_en': album.title_en,
        'release_type': ArtistRelease.TYPE_ALBUM,
        'status': release_status,
        'current_step': 5,
        'track_ids': [song.id for song in songs],
        'tracks': [_legacy_track(song) for song in songs],
        'shared_metadata': merged_shared({}),
        'release_metadata': merged_release_metadata({
            'release_date': album.release_date.isoformat() if album.release_date else '',
            'cover_url': album.cover_image,
            'description': album.description,
            'description_en': album.description_en,
        }, album.artist_id),
        'track_extras': {},
        'validation': {
            'valid': True,
            'errors': [],
            'warnings': [],
            'summary': {
                'release_information': True,
                'artwork': bool(album.cover_image),
                'track_count': len(songs),
                'audio_passed': all(bool(song.audio_file) for song in songs),
                'complete_tracks': len(songs),
                'rights_warnings': 0,
            },
        },
        'created_at': album.created_at,
        'updated_at': album.created_at,
        'submitted_at': None,
    }


def _legacy_single(song, request=None):
    status_map = {
        Song.STATUS_DRAFT: ArtistRelease.STATUS_DRAFT,
        Song.STATUS_PENDING: ArtistRelease.STATUS_IN_REVIEW,
        Song.STATUS_APPROVED: ArtistRelease.STATUS_APPROVED,
        Song.STATUS_REJECTED: ArtistRelease.STATUS_REJECTED,
        Song.STATUS_PUBLISHED: ArtistRelease.STATUS_LIVE,
        Song.STATUS_DELETED: ArtistRelease.STATUS_TAKEN_DOWN,
    }
    release_status = status_map.get(song.status, ArtistRelease.STATUS_DRAFT)
    return {
        'id': f'legacy-song-{song.id}',
        'legacy': True,
        'legacy_kind': 'song',
        'song_id': song.id,
        'album_id': None,
        'title': song.title,
        'title_en': song.title_en,
        'release_type': ArtistRelease.TYPE_SINGLE,
        'status': release_status,
        'current_step': 5,
        'track_ids': [song.id],
        'tracks': [_legacy_track(song)],
        'shared_metadata': merged_shared({}),
        'release_metadata': merged_release_metadata({
            'release_date': song.release_date.isoformat() if song.release_date else '',
            'cover_url': song.cover_image,
            'label': song.label,
            'label_en': song.label_en,
        }, song.artist_id),
        'track_extras': {},
        'validation': {
            'valid': True,
            'errors': [],
            'warnings': [],
            'summary': {
                'release_information': True,
                'artwork': bool(song.cover_image),
                'track_count': 1,
                'audio_passed': bool(song.audio_file),
                'complete_tracks': 1,
                'rights_warnings': 0,
            },
        },
        'created_at': song.created_at,
        'updated_at': song.updated_at,
        'submitted_at': None,
    }


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        queryset = release_queryset().filter(artist=artist)
        status_filter = str(request.query_params.get('status') or '').strip()
        if status_filter:
            values = [item.strip() for item in status_filter.split(',') if item.strip()]
            queryset = queryset.filter(status__in=values)
        query = str(request.query_params.get('q') or '').strip()
        if query:
            queryset = queryset.filter(Q(title__icontains=query) | Q(title_en__icontains=query))
        releases = [serialize_release(item, request) for item in queryset[:250]]

        linked_album_ids = set(ArtistRelease.objects.filter(artist=artist, album__isnull=False).values_list('album_id', flat=True))
        legacy_albums = Album.objects.filter(artist=artist).exclude(id__in=linked_album_ids).prefetch_related(
            Prefetch('songs', queryset=Song.objects.select_related('artist', 'album').order_by('album_track_number', 'id'))
        ).order_by('-created_at')[:100]
        releases.extend(_legacy_release(album, request) for album in legacy_albums)

        linked_song_ids = ArtistReleaseTrack.objects.filter(release__artist=artist).values_list('song_id', flat=True)
        # Any unlinked, album-less recording is a standalone release candidate.
        # Older uploads often left is_single=False, which made their drafts disappear here.
        legacy_singles = Song.objects.filter(
            artist=artist, album__isnull=True,
        ).exclude(id__in=linked_song_ids).select_related('artist').order_by('-updated_at')[:100]
        releases.extend(_legacy_single(song, request) for song in legacy_singles)
        releases.sort(key=lambda item: item.get('updated_at') or item.get('created_at'), reverse=True)
        return Response({'count': len(releases), 'results': releases})

    def post(self, request):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        release_type = str(request.data.get('release_type') or ArtistRelease.TYPE_ALBUM)
        if release_type not in dict(ArtistRelease.TYPE_CHOICES):
            return Response({'release_type': ['نوع انتشار معتبر انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)
        title = str(request.data.get('title') or 'انتشار بدون عنوان').strip() or 'انتشار بدون عنوان'
        title_en = str(request.data.get('title_en') or '').strip()
        if len(title) > 400 or len(title_en) > 400:
            return Response({'title': ['عنوان انتشار نباید بیشتر از ۴۰۰ کاراکتر باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        incoming_release_metadata = request.data.get('release_metadata', {})
        incoming_shared_metadata = request.data.get('shared_metadata', {})
        if not isinstance(incoming_release_metadata, dict):
            return Response({'release_metadata': ['اطلاعات باید به‌صورت یک شیء معتبر ارسال شوند.']}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(incoming_shared_metadata, dict):
            return Response({'shared_metadata': ['اطلاعات باید به‌صورت یک شیء معتبر ارسال شوند.']}, status=status.HTTP_400_BAD_REQUEST)
        metadata = merged_release_metadata(incoming_release_metadata, artist.id)
        metadata['cover_url'] = ''
        release = ArtistRelease.objects.create(
            artist=artist,
            title=title,
            title_en=title_en,
            release_type=release_type,
            previously_released=str(request.data.get('previously_released', '')).strip().lower() in {'1', 'true', 'yes', 'on'},
            shared_metadata=merged_shared(incoming_shared_metadata),
            release_metadata=metadata,
        )
        ArtistReleaseStatusHistory.objects.create(
            release=release,
            from_status='',
            to_status=ArtistRelease.STATUS_DRAFT,
            note='فضای کاری انتشار ایجاد شد.',
            actor=request.user,
        )
        return Response(serialize_release(release_queryset().get(pk=release.pk), request), status=status.HTTP_201_CREATED)


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def _get(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return None
        return get_object_or_404(release_queryset(), pk=pk, artist=artist)

    def get(self, request, pk):
        release = self._get(request, pk)
        if not release:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_release(release, request, include_history=True))

    def patch(self, request, pk):
        release = self._get(request, pk)
        if not release:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            release = ArtistRelease.objects.select_for_update().get(pk=release.pk, artist_id=release.artist_id)
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict

            if str(request.data.get('reopen_for_edit') or '').strip().lower() in {'1', 'true', 'yes', 'on'}:
                if release.status not in {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW}:
                    if not _confirmed_reapproval(request.data):
                        return Response({
                            'detail': 'ویرایش، این انتشار و آهنگ‌های مرتبط را دوباره به وضعیت انتظار بررسی برمی‌گرداند.',
                            'code': 'release_reapproval_required',
                        }, status=status.HTTP_409_CONFLICT)
                    mark_release_for_review(
                        release, actor=request.user, all_tracks=True,
                        note='هنرمند انتشار را برای ویرایش دوباره باز کرد؛ تأیید مجدد لازم است.',
                    )
                release = release_queryset().get(pk=release.pk)
                return Response(serialize_release(release, request, include_history=True))

            locked = _draft_or_409(release)
            if locked:
                return locked

            allowed = {'title', 'title_en', 'release_type', 'previously_released', 'current_step'}
            for field in allowed:
                if field not in request.data:
                    continue
                value = request.data.get(field)
                if field == 'release_type' and value not in dict(ArtistRelease.TYPE_CHOICES):
                    return Response({'release_type': ['نوع انتشار معتبر انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)
                if field in {'title', 'title_en'}:
                    value = str(value or '').strip()
                    if len(value) > 400:
                        return Response({field: ['عنوان را حداکثر در ۴۰۰ کاراکتر وارد کنید.']}, status=status.HTTP_400_BAD_REQUEST)
                    if field == 'title' and not value:
                        value = 'انتشار بدون عنوان'
                if field == 'current_step':
                    try:
                        value = max(1, min(5, int(value)))
                    except (TypeError, ValueError):
                        value = 1
                elif field == 'previously_released':
                    value = value.strip().lower() in {'1', 'true', 'yes', 'on'} if isinstance(value, str) else bool(value)
                setattr(release, field, value)

            if 'shared_metadata' in request.data:
                incoming_shared = request.data.get('shared_metadata')
                if not isinstance(incoming_shared, dict):
                    return Response({'shared_metadata': ['اطلاعات باید به‌صورت یک شیء معتبر ارسال شوند.']}, status=status.HTTP_400_BAD_REQUEST)
                shared = dict(release.shared_metadata or {})
                shared.update(incoming_shared)
                release.shared_metadata = merged_shared(shared)

            if 'release_metadata' in request.data:
                incoming_metadata = request.data.get('release_metadata')
                if not isinstance(incoming_metadata, dict):
                    return Response({'release_metadata': ['اطلاعات باید به‌صورت یک شیء معتبر ارسال شوند.']}, status=status.HTTP_400_BAD_REQUEST)
                existing_cover = str((release.release_metadata or {}).get('cover_url') or '')
                metadata = dict(release.release_metadata or {})
                metadata.update(incoming_metadata)
                release.release_metadata = merged_release_metadata(metadata, release.artist_id)
                release.release_metadata['cover_url'] = existing_cover

            if 'track_extras' in request.data:
                if not isinstance(request.data.get('track_extras'), dict):
                    return Response({'track_extras': ['اطلاعات را به‌صورت شیئی با کلید شناسه آهنگ ارسال کنید.']}, status=status.HTTP_400_BAD_REQUEST)
                extras_map = request.data.get('track_extras') or {}
                for link in release.release_tracks.all():
                    value = extras_map.get(str(link.song_id), extras_map.get(link.song_id))
                    if isinstance(value, dict):
                        combined_extras = dict(link.extras or {})
                        combined_extras.update(value)
                        link.extras = normalize_track_extras(combined_extras, link.position)
                        link.save(update_fields=['extras', 'updated_at'])

            release.validation_snapshot = {}
            release.lock_version += 1
            release.save()
            sync_release_tracks(release)
            if release.status == ArtistRelease.STATUS_IN_REVIEW:
                Song.objects.filter(release_track_links__release=release).exclude(status=Song.STATUS_DELETED).update(status=Song.STATUS_PENDING)

        return Response(serialize_release(release_queryset().get(pk=release.pk), request))

    put = patch

    def delete(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)

        media_urls = []
        with transaction.atomic():
            release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk, artist=artist)
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict

            links = list(
                ArtistReleaseTrack.objects.select_for_update()
                .select_related('song')
                .filter(release=release)
            )
            song_ids = [link.song_id for link in links]
            shared_song_ids = set(
                ArtistReleaseTrack.objects.filter(song_id__in=song_ids)
                .exclude(release=release)
                .values_list('song_id', flat=True)
            )
            preserved_link_ids = []
            hard_delete_ids = []
            shared_detach_ids = []
            for link in links:
                song = link.song
                if link.song_id in shared_song_ids:
                    shared_detach_ids.append(link.song_id)
                    continue
                has_accounting = bool(song.plays) or song.play_counts.exists()
                must_preserve = (
                    release.status != ArtistRelease.STATUS_DRAFT
                    or song.status in {Song.STATUS_PUBLISHED, Song.STATUS_DELETED}
                    or has_accounting
                )
                if must_preserve:
                    if song.status != Song.STATUS_DELETED:
                        song.status = Song.STATUS_DELETED
                        song.save(update_fields=['status', 'updated_at'])
                    preserved_link_ids.append(link.pk)
                else:
                    hard_delete_ids.append(song.pk)
                    media_urls.extend(filter(None, [
                        song.audio_file, song.converted_audio_url,
                        song.preview_audio_url, song.cover_image,
                    ]))

            removable_link_ids = [link.pk for link in links if link.pk not in preserved_link_ids]
            if removable_link_ids:
                ArtistReleaseTrack.objects.filter(pk__in=removable_link_ids).delete()

            album = release.album
            if album and shared_detach_ids:
                Song.objects.filter(pk__in=shared_detach_ids, album=album).update(album=None, is_single=True)
            if hard_delete_ids:
                Song.objects.filter(pk__in=hard_delete_ids).delete()

            if preserved_link_ids:
                _renumber_release_links(release)
                _set_release_taken_down(
                    release, request.user,
                    'انتشار توسط هنرمند حذف شد و فایل‌های ضبط‌شده و سوابق مالی آن محفوظ ماند.',
                )
                response_release = release_queryset().get(pk=release.pk)
                payload = {
                    'deletion': 'soft',
                    'message': 'انتشار غیرفعال شد و ترک‌ها، آمار و درآمدهای قبلی آن محفوظ ماند.',
                    'release': serialize_release(response_release, request),
                }
            else:
                raw_cover = str((release.release_metadata or {}).get('cover_url') or '')
                if raw_cover:
                    media_urls.append(raw_cover)
                release.delete()
                if album and not album.songs.exists():
                    if album.cover_image:
                        media_urls.append(album.cover_image)
                    album.delete()
                payload = {
                    'deletion': 'hard',
                    'message': 'انتشار و فایل‌های ضبط‌شده قابل حذف آن برای همیشه پاک شدند.',
                }

            if media_urls:
                transaction.on_commit(lambda values=tuple(media_urls): _cleanup_unreferenced_release_media(values))
            return Response(payload)


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseTracksView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk, artist=artist)
            locked = _draft_or_409(release)
            if locked:
                return locked
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict

            action = str(request.data.get('action') or 'add')
            if action == 'reorder':
                ordered_ids = _id_list(request.data.get('ordered_song_ids'))
                links = list(ArtistReleaseTrack.objects.select_for_update().filter(release=release).order_by('position', 'id'))
                current_ids = [item.song_id for item in links]
                if len(ordered_ids) != len(current_ids) or set(ordered_ids) != set(current_ids):
                    return Response({'ordered_song_ids': ['ترتیب ارسالی باید هر ترک انتشار را دقیقاً یک‌بار شامل شود.']}, status=status.HTTP_400_BAD_REQUEST)
                link_map = {item.song_id: item for item in links}
                for offset, song_id in enumerate(ordered_ids, start=1):
                    ArtistReleaseTrack.objects.filter(pk=link_map[song_id].pk).update(position=1000 + offset)
                for offset, song_id in enumerate(ordered_ids, start=1):
                    ArtistReleaseTrack.objects.filter(pk=link_map[song_id].pk).update(position=offset)
                sync_release_tracks(release)
                _touch_release(release)
            elif action == 'add':
                song_ids = _id_list(request.data.get('song_ids'))
                if not song_ids:
                    return Response({'song_ids': ['حداقل یک فایل ضبط‌شده انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)
                songs = {song.id: song for song in Song.objects.filter(
                    id__in=song_ids, artist=artist
                ).exclude(status=Song.STATUS_DELETED).prefetch_related(
                    'featured_artists', 'genres', 'sub_genres', 'moods', 'tags'
                )}
                missing = [song_id for song_id in song_ids if song_id not in songs]
                if missing:
                    return Response({'song_ids': [f'برخی فایل‌های ضبط‌شده در دسترس نیستند یا متعلق به این هنرمند نیستند: {missing}']}, status=status.HTTP_400_BAD_REQUEST)

                existing_links = list(ArtistReleaseTrack.objects.select_for_update().filter(release=release))
                existing_ids = {item.song_id for item in existing_links}
                existing_source_ids = {item.source_song_id for item in existing_links if item.source_song_id}
                candidates = [song_id for song_id in song_ids if song_id not in existing_ids and song_id not in existing_source_ids]
                if len(existing_links) + len(candidates) > 100:
                    return Response({'song_ids': ['هر انتشار می‌تواند حداکثر ۱۰۰ ترک داشته باشد.']}, status=status.HTTP_400_BAD_REQUEST)
                position = len(existing_links)
                for song_id in candidates:
                    editable_song, source = ensure_editable_song(release, songs[song_id], uploader=request.user)
                    position += 1
                    ArtistReleaseTrack.objects.create(
                        release=release,
                        song=editable_song,
                        source_song=source,
                        position=position,
                        extras={},
                    )
                    if release.status == ArtistRelease.STATUS_IN_REVIEW and editable_song.status != Song.STATUS_DELETED:
                        Song.objects.filter(pk=editable_song.pk).update(status=Song.STATUS_PENDING)
                if candidates:
                    sync_release_tracks(release)
                    _touch_release(release)
            else:
                return Response({'action': ['یکی از عملیات افزودن یا تغییر ترتیب را انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)

        return Response(serialize_release(release_queryset().get(pk=release.pk), request))

    def delete(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        song_ids = _id_list(request.data.get('song_ids'))
        if not song_ids:
            return Response({'song_ids': ['حداقل یک ترک از انتشار انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)

        media_urls = []
        with transaction.atomic():
            release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk, artist=artist)
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict

            links = list(
                ArtistReleaseTrack.objects.select_for_update()
                .select_related('song')
                .filter(release=release, song_id__in=song_ids)
            )
            removed_ids = [link.song_id for link in links]
            album = release.album
            if links:
                ArtistReleaseTrack.objects.filter(pk__in=[link.pk for link in links]).delete()
                if album:
                    Song.objects.filter(id__in=removed_ids, album=album).update(album=None, is_single=True)
                if release.status == ArtistRelease.STATUS_DRAFT:
                    Song.objects.filter(id__in=removed_ids).exclude(
                        status=Song.STATUS_DELETED
                    ).exclude(release_track_links__isnull=False).update(status=Song.STATUS_DRAFT)
                _renumber_release_links(release)

            album_deleted = False
            album_deletion = None
            if album and not album.songs.exclude(status=Song.STATUS_DELETED).exists():
                album_deleted = True
                if album.songs.exists():
                    album_deletion = 'soft'
                else:
                    album_deletion = 'hard'
                    if album.cover_image:
                        media_urls.append(album.cover_image)
                    album.delete()

            no_active_tracks = not release.release_tracks.exclude(song__status=Song.STATUS_DELETED).exists()
            delete_empty_album = str(request.data.get('delete_empty_album') or '').strip().lower() in {'1', 'true', 'yes', 'on'}
            release_deleted = False
            if no_active_tracks and delete_empty_album and release.status == ArtistRelease.STATUS_DRAFT and release.release_type == ArtistRelease.TYPE_ALBUM:
                raw_cover = str((release.release_metadata or {}).get('cover_url') or '')
                if raw_cover:
                    media_urls.append(raw_cover)
                release.delete()
                release_deleted = True
            elif no_active_tracks and release.status != ArtistRelease.STATUS_DRAFT:
                _set_release_taken_down(
                    release, request.user,
                    'آخرین ترک فعال این انتشار توسط هنرمند حذف شد.',
                )
            else:
                _touch_release(release)

            if media_urls:
                transaction.on_commit(lambda values=tuple(media_urls): _cleanup_unreferenced_release_media(values))
            if release_deleted:
                return Response({
                    'removed_ids': removed_ids,
                    'album_deleted': True,
                    'album_deletion': 'hard',
                    'release_deleted': True,
                })
            result = serialize_release(release_queryset().get(pk=release.pk), request)
            result.update({
                'removed_ids': removed_ids,
                'album_deleted': album_deleted,
                'album_deletion': album_deletion,
                'release_deleted': False,
            })
            return Response(result)


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseBulkMetadataView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        metadata = request.data.get('metadata')
        song_ids = _id_list(request.data.get('song_ids'))
        if not isinstance(metadata, dict) or not song_ids:
            return Response(
                {'detail': 'اطلاعات اثر را به‌صورت دستی همراه با حداقل یک ترک انتشار وارد کنید.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if 'rows' in request.data or 'copy_from_song_id' in request.data:
            return Response(
                {'detail': 'ورود فایل و کپی خودکار اطلاعات دیگر پشتیبانی نمی‌شود. اطلاعات را به‌صورت دستی وارد کنید.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk, artist=artist)
            locked = _draft_or_409(release)
            if locked:
                return locked
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            links = {
                item.song_id: item
                for item in ArtistReleaseTrack.objects.select_for_update().select_related('song').filter(
                    release=release, song_id__in=song_ids
                )
            }
            for song_id in song_ids:
                if song_id in links:
                    apply_track_metadata(links[song_id].song, metadata)
            if not links:
                return Response({'detail': 'هیچ ترک منطبقی برای به‌روزرسانی پیدا نشد.'}, status=status.HTTP_400_BAD_REQUEST)
            _touch_release(release)

        return Response(serialize_release(release_queryset().get(pk=release.pk), request))


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseArtworkView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        artist = _artist_for_user(request.user)
        release = get_object_or_404(release_queryset(), pk=pk, artist=artist) if artist else None
        if not release:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        if release.status not in {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW} and not _confirmed_reapproval(request.data):
            return Response({
                'detail': 'تغییر کاور، انتشار و آهنگ‌های آن را دوباره به وضعیت انتظار بررسی برمی‌گرداند.',
                'code': 'release_reapproval_required',
            }, status=status.HTTP_409_CONFLICT)
        conflict = _lock_version_error(release, request.data)
        if conflict:
            return conflict
        image_file = request.FILES.get('cover_image')
        if not image_file:
            return Response({'cover_image': ['انتخاب تصویر کاور الزامی است.']}, status=status.HTTP_400_BAD_REQUEST)
        if image_file.size > 10 * 1024 * 1024:
            return Response({'cover_image': ['حجم تصویر کاور باید کمتر از ۱۰ مگابایت باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        if getattr(image_file, 'content_type', '') not in {'image/jpeg', 'image/png', 'image/webp'}:
            return Response({'cover_image': ['فرمت تصویر کاور باید JPG، PNG یا WEBP باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        try:
            image_file.seek(0)
            with Image.open(image_file) as image:
                width, height = image.size
            image_file.seek(0)
        except Exception:
            return Response({'cover_image': ['تصویر کاور آسیب‌دیده یا غیرقابل خواندن است.']}, status=status.HTTP_400_BAD_REQUEST)
        if width != height:
            return Response({'cover_image': ['تصویر کاور باید مربعی باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        try:
            url, _ = upload_file_to_r2(image_file, folder='covers/releases')
        except MediaPipelineError as exc:
            return Response({'cover_image': [str(exc)], 'code': exc.code}, status=exc.status_code)

        old_url = ''
        try:
            with transaction.atomic():
                release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=release.pk, artist=artist)
                conflict = _lock_version_error(release, request.data)
                if conflict:
                    cleanup_r2_urls([url])
                    return conflict
                if release.status not in {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW}:
                    if not _confirmed_reapproval(request.data):
                        cleanup_r2_urls([url])
                        return Response({
                            'detail': 'تغییر کاور نیازمند تأیید ارسال دوباره برای بررسی است.',
                            'code': 'release_reapproval_required',
                        }, status=status.HTTP_409_CONFLICT)
                    mark_release_for_review(
                        release, actor=request.user, all_tracks=True,
                        note='هنرمند کاور انتشار را تغییر داد؛ تأیید مجدد لازم است.',
                    )
                    release = ArtistRelease.objects.select_for_update().get(pk=release.pk)
                metadata = merged_release_metadata(release.release_metadata, release.artist_id)
                old_url = str(metadata.get('cover_url') or '')
                metadata['cover_url'] = url
                release.release_metadata = metadata
                release.lock_version += 1
                release.validation_snapshot = {}
                release.save(update_fields=['release_metadata', 'lock_version', 'validation_snapshot', 'updated_at'])
                # Persist the release artwork onto song rows immediately. Singles
                # always share the release cover; collection tracks keep their own
                # cover when one was explicitly uploaded for that track.
                sync_release_artwork(release, previous_cover=old_url)
                if release.status == ArtistRelease.STATUS_IN_REVIEW:
                    Song.objects.filter(release_track_links__release=release).exclude(
                        status=Song.STATUS_DELETED
                    ).update(status=Song.STATUS_PENDING)
        except Exception:
            cleanup_r2_urls([url])
            logger.exception('Release artwork save failed release=%s user=%s', pk, request.user.pk)
            return Response(
                {'detail': 'کاور بارگذاری شد، اما اتصال آن به انتشار انجام نشد.', 'code': 'artwork_save_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if (
            old_url and old_url != url
            and not ArtistRelease.objects.filter(release_metadata__cover_url=old_url).exists()
            and not Song.objects.filter(cover_image=old_url).exists()
            and not Album.objects.filter(cover_image=old_url).exists()
        ):
            cleanup_r2_urls([old_url])
        return Response(serialize_release(release_queryset().get(pk=release.pk), request))


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseValidateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            locked_release = get_object_or_404(ArtistRelease.objects.select_for_update().only('pk'), pk=pk, artist=artist)
            release = release_queryset().get(pk=locked_release.pk)
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            sync_release_tracks(release)
            release = release_queryset().get(pk=release.pk)
            validation = validation_payload(release)
            release.validation_snapshot = validation
            release.save(update_fields=['validation_snapshot', 'updated_at'])
        return Response(validation)


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseSubmitView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            locked_release = get_object_or_404(ArtistRelease.objects.select_for_update().only('pk'), pk=pk, artist=artist)
            release = release_queryset().get(pk=locked_release.pk)
            if release.status != ArtistRelease.STATUS_DRAFT:
                return Response(
                    {'detail': 'این انتشار هم‌اکنون در حال بررسی است.', 'code': 'release_already_in_review'},
                    status=status.HTTP_409_CONFLICT,
                )
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            sync_release_tracks(release)
            release = release_queryset().get(pk=release.pk)
            validation = validation_payload(release)
            release.validation_snapshot = validation
            release.save(update_fields=['validation_snapshot', 'updated_at'])
            if not validation['valid']:
                return Response({'detail': 'اعتبارسنجی انتشار انجام نشد. خطاهای مشخص‌شده را اصلاح کنید.', 'validation': validation}, status=status.HTTP_400_BAD_REQUEST)
            links = list(ArtistReleaseTrack.objects.select_for_update().select_related('song').filter(release=release).order_by('position', 'id'))
            for link in links:
                link.metadata_snapshot = snapshot_song(link.song)
                link.save(update_fields=['metadata_snapshot', 'updated_at'])
                Song.objects.filter(pk=link.song_id).exclude(status=Song.STATUS_DELETED).update(
                    status=Song.STATUS_PENDING,
                    is_single=release.release_type == ArtistRelease.TYPE_SINGLE,
                )
            release.submitted_at = timezone.now()
            release.current_step = 5
            release.save(update_fields=['submitted_at', 'current_step', 'updated_at'])
            change_status(release, ArtistRelease.STATUS_IN_REVIEW, actor=request.user, note='انتشار توسط هنرمند برای بررسی ارسال شد.')
        return Response(serialize_release(release_queryset().get(pk=release.pk), request))


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseCloneView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        artist = _artist_for_user(request.user)
        release = get_object_or_404(release_queryset(), pk=pk, artist=artist) if artist else None
        if not release:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        mode = str(request.data.get('mode') or 'duplicate')
        if mode not in {'duplicate', 'revision'}:
            return Response({'mode': ['یکی از گزینه‌های نسخه تکراری یا ویرایش جدید را انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)
        copy = create_revision(release, uploader=request.user, mode=mode)
        return Response(serialize_release(copy, request), status=status.HTTP_201_CREATED)


@extend_schema(tags=['Artist Releases'])
class ArtistContributorListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        rows = ReleaseContributor.objects.filter(artist=artist)
        return Response({'count': rows.count(), 'results': [
            {'id': item.id, 'name': item.name, 'name_en': item.name_en, 'roles': item.roles, 'created_at': item.created_at, 'updated_at': item.updated_at}
            for item in rows
        ]})

    def post(self, request):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'پروفایل هنرمند پیدا نشد.'}, status=status.HTTP_404_NOT_FOUND)
        name = str(request.data.get('name') or '').strip()
        if not name:
            return Response({'name': ['وارد کردن نام مشارکت‌کننده الزامی است.']}, status=status.HTTP_400_BAD_REQUEST)
        if len(name) > 255:
            return Response({'name': ['نام مشارکت‌کننده نباید بیشتر از ۲۵۵ کاراکتر باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        name_en = str(request.data.get('name_en') or '').strip()
        if len(name_en) > 255:
            return Response({'name_en': ['نام انگلیسی مشارکت‌کننده نباید بیشتر از ۲۵۵ کاراکتر باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        incoming_roles = request.data.get('roles', [])
        if not isinstance(incoming_roles, list):
            return Response({'roles': ['فهرست نقش‌های مشارکت‌کننده را ارسال کنید.']}, status=status.HTTP_400_BAD_REQUEST)
        allowed_roles = {'producer', 'composer', 'lyricist', 'songwriter', 'performer', 'engineer', 'remixer', 'other'}
        roles = []
        for value in incoming_roles:
            role = str(value or '').strip().lower()
            if role and role not in roles:
                roles.append(role)
        invalid_roles = [role for role in roles if role not in allowed_roles]
        if invalid_roles:
            return Response({'roles': ['یک یا چند نقش انتخاب‌شده برای مشارکت‌کننده پشتیبانی نمی‌شود.']}, status=status.HTTP_400_BAD_REQUEST)
        if len(roles) > 10:
            return Response({'roles': ['هر مشارکت‌کننده می‌تواند حداکثر ۱۰ نقش داشته باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        contributor, created = ReleaseContributor.objects.get_or_create(
            artist=artist,
            name=name,
            defaults={'name_en': name_en, 'roles': roles},
        )
        if not created:
            contributor.name_en = name_en or contributor.name_en
            contributor.roles = roles or contributor.roles
            contributor.save()
        return Response({
            'id': contributor.id,
            'name': contributor.name,
            'name_en': contributor.name_en,
            'roles': contributor.roles,
            'created_at': contributor.created_at,
            'updated_at': contributor.updated_at,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@extend_schema(tags=['Admin Releases'])
class AdminReleaseListView(APIView):
    permission_classes = [IsAdminPanelUser]

    def get(self, request):
        publish_due_releases()
        queryset = release_queryset()
        if is_employee(request.user):
            can_add = has_employee_permission(request.user, 'release_add.view')
            can_review = has_employee_permission(request.user, 'releases.view')
            admin_created = Q(
                status_history__from_status='',
                status_history__to_status=ArtistRelease.STATUS_DRAFT,
                status_history__note=ADMIN_CREATED_HISTORY_NOTE,
            )
            if can_add and can_review:
                queryset = queryset.filter(~Q(status=ArtistRelease.STATUS_DRAFT) | admin_created).distinct()
            elif can_add:
                queryset = queryset.filter(admin_created).distinct()
            elif can_review:
                queryset = queryset.exclude(status=ArtistRelease.STATUS_DRAFT)
            else:
                queryset = queryset.none()
        query = str(request.query_params.get('q') or '').strip()
        status_filter = str(request.query_params.get('status') or '').strip()
        if not status_filter:
            queryset = queryset.exclude(status=ArtistRelease.STATUS_DRAFT)
        artist_id = request.query_params.get('artist_id')
        if query:
            queryset = queryset.filter(Q(title__icontains=query) | Q(title_en__icontains=query) | Q(artist__name__icontains=query) | Q(artist__artistic_name__icontains=query))
        if status_filter:
            queryset = queryset.filter(status__in=[item.strip() for item in status_filter.split(',') if item.strip()])
        if artist_id:
            try:
                artist_id_value = int(artist_id)
            except (TypeError, ValueError):
                return Response({'artist_id': ['شناسه هنرمند معتبر نیست.']}, status=status.HTTP_400_BAD_REQUEST)
            if artist_id_value <= 0:
                return Response({'artist_id': ['شناسه هنرمند معتبر نیست.']}, status=status.HTTP_400_BAD_REQUEST)
            queryset = queryset.filter(artist_id=artist_id_value)
        try:
            page = max(1, int(request.query_params.get('page', 1)))
            page_size = max(1, min(100, int(request.query_params.get('page_size', 20))))
        except (TypeError, ValueError):
            page, page_size = 1, 20
        count = queryset.count()
        start = (page - 1) * page_size
        rows = queryset[start:start + page_size]
        return Response({'count': count, 'page': page, 'page_size': page_size, 'results': [serialize_release(item, request) for item in rows]})

    def post(self, request):
        try:
            artist_id = int(request.data.get('artist_id') or 0)
        except (TypeError, ValueError):
            artist_id = 0
        artist = Artist.objects.filter(pk=artist_id).first()
        if not artist:
            return Response({'artist_id': ['هنرمند معتبر انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)
        release_type = str(request.data.get('release_type') or ArtistRelease.TYPE_SINGLE).strip()
        if release_type not in dict(ArtistRelease.TYPE_CHOICES):
            return Response({'release_type': ['نوع انتشار معتبر انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)
        title = str(request.data.get('title') or '').strip()
        title_en = str(request.data.get('title_en') or '').strip()
        if not title:
            return Response({'title': ['عنوان فارسی انتشار الزامی است.']}, status=status.HTTP_400_BAD_REQUEST)
        if len(title) > 400 or len(title_en) > 400:
            return Response({'title': ['عنوان انتشار نباید بیشتر از ۴۰۰ کاراکتر باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        shared = request.data.get('shared_metadata') or {}
        metadata = request.data.get('release_metadata') or {}
        if not isinstance(shared, dict) or not isinstance(metadata, dict):
            return Response({'detail': 'متادیتای انتشار باید به‌صورت شیء معتبر ارسال شود.'}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            release = ArtistRelease.objects.create(
                artist=artist,
                title=title,
                title_en=title_en,
                release_type=release_type,
                previously_released=str(request.data.get('previously_released') or '').strip().lower() in {'1','true','yes','on'},
                shared_metadata=merged_shared(shared),
                release_metadata=merged_release_metadata(metadata, artist.id),
                current_step=1,
            )
            release.release_metadata['cover_url'] = ''
            release.save(update_fields=['release_metadata', 'updated_at'])
            ArtistReleaseStatusHistory.objects.create(
                release=release, from_status='', to_status=ArtistRelease.STATUS_DRAFT,
                note=ADMIN_CREATED_HISTORY_NOTE, actor=request.user,
            )
        return Response(serialize_release(release_queryset().get(pk=release.pk), request, include_history=True), status=status.HTTP_201_CREATED)


@extend_schema(tags=['Admin Releases'])
class AdminReleaseDetailView(APIView):
    permission_classes = [IsAdminPanelUser]

    def get(self, request, pk):
        release = get_object_or_404(release_queryset(), pk=pk)
        _require_employee_release_read(request.user, release)
        return Response(serialize_release(release, request, include_history=True))

    def patch(self, request, pk):
        with transaction.atomic():
            release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk)
            _require_employee_admin_release(request.user, release, 'release_add.edit')
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            editable = {
                ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW,
                ArtistRelease.STATUS_CHANGES_REQUESTED, ArtistRelease.STATUS_APPROVED,
            }
            content_fields = {'title','title_en','release_type','previously_released','current_step','shared_metadata','release_metadata','track_extras'}
            if any(field in request.data for field in content_fields) and release.status not in editable:
                return Response(
                    {'detail': 'این انتشار در وضعیت فعلی برای ویرایش محتوایی قفل است. ابتدا آن را بازگشایی کنید.'},
                    status=status.HTTP_409_CONFLICT,
                )

            for field in ('title','title_en','release_type','previously_released','current_step'):
                if field not in request.data:
                    continue
                value = request.data.get(field)
                if field == 'release_type':
                    if value not in dict(ArtistRelease.TYPE_CHOICES):
                        return Response({'release_type': ['نوع انتشار معتبر انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)
                elif field in {'title','title_en'}:
                    value = str(value or '').strip()
                    if field == 'title' and not value:
                        return Response({'title': ['عنوان فارسی انتشار الزامی است.']}, status=status.HTTP_400_BAD_REQUEST)
                    if len(value) > 400:
                        return Response({field: ['عنوان را حداکثر در ۴۰۰ کاراکتر وارد کنید.']}, status=status.HTTP_400_BAD_REQUEST)
                elif field == 'current_step':
                    try:
                        value = max(1, min(5, int(value)))
                    except (TypeError, ValueError):
                        value = release.current_step
                elif field == 'previously_released':
                    value = value.strip().lower() in {'1','true','yes','on'} if isinstance(value,str) else bool(value)
                setattr(release, field, value)

            if 'shared_metadata' in request.data:
                incoming = request.data.get('shared_metadata')
                if not isinstance(incoming, dict):
                    return Response({'shared_metadata': ['اطلاعات باید به‌صورت یک شیء معتبر ارسال شوند.']}, status=status.HTTP_400_BAD_REQUEST)
                shared = dict(release.shared_metadata or {})
                shared.update(incoming)
                release.shared_metadata = merged_shared(shared)

            if 'release_metadata' in request.data:
                incoming = request.data.get('release_metadata')
                if not isinstance(incoming, dict):
                    return Response({'release_metadata': ['اطلاعات باید به‌صورت یک شیء معتبر ارسال شوند.']}, status=status.HTTP_400_BAD_REQUEST)
                old_cover = str((release.release_metadata or {}).get('cover_url') or '')
                metadata = dict(release.release_metadata or {})
                metadata.update(incoming)
                metadata = merged_release_metadata(metadata, release.artist_id)
                metadata['cover_url'] = old_cover
                release.release_metadata = metadata

            if 'track_extras' in request.data:
                extras_map = request.data.get('track_extras')
                if not isinstance(extras_map, dict):
                    return Response({'track_extras': ['اطلاعات ترک باید به‌صورت شیء معتبر ارسال شود.']}, status=status.HTTP_400_BAD_REQUEST)
                for link in ArtistReleaseTrack.objects.select_for_update().filter(release=release):
                    incoming = extras_map.get(str(link.song_id), extras_map.get(link.song_id))
                    if isinstance(incoming, dict):
                        combined = dict(link.extras or {})
                        combined.update(incoming)
                        link.extras = normalize_track_extras(combined, link.position)
                        link.save(update_fields=['extras','updated_at'])

            if 'admin_note' in request.data:
                release.admin_note = str(request.data.get('admin_note') or '')
            if 'review_note' in request.data:
                release.review_note = str(request.data.get('review_note') or '')

            release.validation_snapshot = {}
            release.lock_version += 1
            release.save()
            sync_release_tracks(release)
            if release.status in {ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_CHANGES_REQUESTED}:
                Song.objects.filter(release_track_links__release=release).exclude(status=Song.STATUS_DELETED).update(status=Song.STATUS_PENDING)

        return Response(serialize_release(release_queryset().get(pk=release.pk), request, include_history=True))


    def delete(self, request, pk):
        """Permanently erase one release and data owned exclusively by it.

        Recordings reused by another release are detached rather than destroyed,
        preventing a destructive admin action from corrupting unrelated catalog
        content. Unique recordings are deleted normally so their dependent
        likes/history/stream relations are cascaded by the existing model rules.
        """
        media_urls = []
        orphan_play_ids = []
        with transaction.atomic():
            release = get_object_or_404(
                ArtistRelease.objects.select_for_update().select_related('album'),
                pk=pk,
            )
            if is_employee(request.user) and release.status == ArtistRelease.STATUS_DRAFT:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied('حذف دائمی پیش‌نویس از جریان بررسی انتشارها مجاز نیست.')
            links = list(
                ArtistReleaseTrack.objects.select_for_update()
                .select_related('song')
                .filter(release=release)
            )
            song_ids = [link.song_id for link in links]
            shared_song_ids = set(
                ArtistReleaseTrack.objects.filter(song_id__in=song_ids)
                .exclude(release=release)
                .values_list('song_id', flat=True)
            )
            unique_songs = [link.song for link in links if link.song_id not in shared_song_ids]
            unique_song_ids = [song.pk for song in unique_songs]

            for song in unique_songs:
                media_urls.extend(filter(None, [
                    song.audio_file,
                    song.converted_audio_url,
                    song.preview_audio_url,
                    song.cover_image,
                ]))
                orphan_play_ids.extend(song.play_counts.values_list('pk', flat=True))

            raw_cover = str((release.release_metadata or {}).get('cover_url') or '').strip()
            if raw_cover:
                media_urls.append(raw_cover)
            album = release.album

            # Remove this release's links/history/workspace first. Shared songs
            # remain valid because their other release links are untouched.
            release.delete()
            if unique_song_ids:
                Song.objects.filter(pk__in=unique_song_ids).delete()
            if orphan_play_ids:
                PlayCount.objects.filter(pk__in=set(orphan_play_ids), songs__isnull=True).delete()

            album_deleted = False
            if album and not album.songs.exists():
                if album.cover_image:
                    media_urls.append(album.cover_image)
                album.delete()
                album_deleted = True

            if media_urls:
                transaction.on_commit(
                    lambda values=tuple(media_urls): _cleanup_unreferenced_release_media(values)
                )

        return Response({
            'deleted': True,
            'release_id': str(pk),
            'deleted_recordings': len(unique_song_ids),
            'preserved_shared_recordings': len(shared_song_ids),
            'album_deleted': album_deleted,
        })



@extend_schema(tags=['Admin Releases'])
class AdminReleaseTracksView(APIView):
    """Admin-owned tracklist editor using the same release workspace semantics as artists."""
    permission_classes = [IsAdminPanelUser]

    def post(self, request, pk):
        with transaction.atomic():
            release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk)
            _require_employee_admin_release(request.user, release, 'release_add.edit')
            if release.status != ArtistRelease.STATUS_DRAFT:
                return Response({'detail': 'برای تغییر ترک‌لیست، انتشار باید در وضعیت پیش‌نویس باشد.'}, status=status.HTTP_409_CONFLICT)
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            action = str(request.data.get('action') or 'add').strip()
            if action == 'reorder':
                ordered_ids = _id_list(request.data.get('ordered_song_ids'))
                links = list(ArtistReleaseTrack.objects.select_for_update().filter(release=release).order_by('position','id'))
                current_ids = [link.song_id for link in links]
                if len(ordered_ids) != len(current_ids) or set(ordered_ids) != set(current_ids):
                    return Response({'ordered_song_ids': ['ترتیب ارسالی باید همه ترک‌های انتشار را دقیقاً یک‌بار شامل شود.']}, status=status.HTTP_400_BAD_REQUEST)
                link_map = {link.song_id: link for link in links}
                for offset, song_id in enumerate(ordered_ids, 1):
                    ArtistReleaseTrack.objects.filter(pk=link_map[song_id].pk).update(position=1000 + offset)
                for offset, song_id in enumerate(ordered_ids, 1):
                    ArtistReleaseTrack.objects.filter(pk=link_map[song_id].pk).update(position=offset)
                sync_release_tracks(release)
                _touch_release(release)
            elif action == 'add':
                song_ids = _id_list(request.data.get('song_ids'))
                if not song_ids:
                    return Response({'song_ids': ['حداقل یک ضبط انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)
                songs = {song.id: song for song in Song.objects.filter(id__in=song_ids, artist=release.artist).exclude(status=Song.STATUS_DELETED)}
                missing = [song_id for song_id in song_ids if song_id not in songs]
                if missing:
                    return Response({'song_ids': [f'برخی ضبط‌ها در دسترس نیستند یا متعلق به این هنرمند نیستند: {missing}']}, status=status.HTTP_400_BAD_REQUEST)
                links = list(ArtistReleaseTrack.objects.select_for_update().filter(release=release))
                existing_ids = {link.song_id for link in links}
                existing_source_ids = {link.source_song_id for link in links if link.source_song_id}
                candidates = [song_id for song_id in song_ids if song_id not in existing_ids and song_id not in existing_source_ids]
                next_count = len(links) + len(candidates)
                if release.release_type == ArtistRelease.TYPE_SINGLE and next_count > 1:
                    return Response({'song_ids': ['تک‌آهنگ فقط می‌تواند یک ترک داشته باشد.']}, status=status.HTTP_400_BAD_REQUEST)
                if release.release_type == ArtistRelease.TYPE_EP and next_count > 7:
                    return Response({'song_ids': ['مینی‌آلبوم می‌تواند حداکثر ۷ ترک داشته باشد.']}, status=status.HTTP_400_BAD_REQUEST)
                if next_count > 100:
                    return Response({'song_ids': ['هر انتشار می‌تواند حداکثر ۱۰۰ ترک داشته باشد.']}, status=status.HTTP_400_BAD_REQUEST)
                position = len(links)
                for song_id in candidates:
                    editable, source = ensure_editable_song(release, songs[song_id], uploader=request.user)
                    position += 1
                    ArtistReleaseTrack.objects.create(release=release, song=editable, source_song=source, position=position, extras={})
                if candidates:
                    sync_release_tracks(release)
                    _touch_release(release)
            else:
                return Response({'action': ['عملیات ترک‌لیست معتبر نیست.']}, status=status.HTTP_400_BAD_REQUEST)
        return Response(serialize_release(release_queryset().get(pk=release.pk), request, include_history=True))

    def delete(self, request, pk):
        song_ids = _id_list(request.data.get('song_ids'))
        if not song_ids:
            return Response({'song_ids': ['حداقل یک ترک انتخاب کنید.']}, status=status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk)
            _require_employee_admin_release(request.user, release, 'release_add.edit')
            if release.status != ArtistRelease.STATUS_DRAFT:
                return Response({'detail': 'برای حذف ترک، انتشار باید در وضعیت پیش‌نویس باشد.'}, status=status.HTTP_409_CONFLICT)
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            links = list(ArtistReleaseTrack.objects.select_for_update().filter(release=release, song_id__in=song_ids))
            if links:
                ArtistReleaseTrack.objects.filter(pk__in=[link.pk for link in links]).delete()
                _renumber_release_links(release)
                _touch_release(release)
        result = serialize_release(release_queryset().get(pk=release.pk), request, include_history=True)
        result['removed_ids'] = [link.song_id for link in links]
        return Response(result)


@extend_schema(tags=['Admin Releases'])
class AdminReleaseTrackUploadView(APIView):
    permission_classes = [IsAdminPanelUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        release = get_object_or_404(ArtistRelease.objects.select_related('artist'), pk=pk)
        _require_employee_admin_release(request.user, release, 'release_add.edit')
        if release.status != ArtistRelease.STATUS_DRAFT:
            return Response({'detail': 'بارگذاری ترک جدید فقط برای پیش‌نویس انتشار مجاز است.'}, status=status.HTTP_409_CONFLICT)
        audio_file = request.FILES.get('audio_file')
        if not audio_file:
            return Response({'audio_file': ['فایل صوتی الزامی است.']}, status=status.HTTP_400_BAD_REQUEST)
        title = str(request.data.get('title') or '').strip()
        title_en = str(request.data.get('title_en') or '').strip()
        if not title and not title_en:
            return Response({'title': ['حداقل عنوان فارسی یا انگلیسی ترک را وارد کنید.']}, status=status.HTTP_400_BAD_REQUEST)
        title = title or title_en
        if len(title) > 400 or len(title_en) > 400:
            return Response({'title': ['عنوان ترک نباید بیشتر از ۴۰۰ کاراکتر باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        if audio_file.size <= 0 or audio_file.size > 500 * 1024 * 1024:
            return Response({'audio_file': ['حجم فایل صوتی باید بیشتر از صفر و کمتر از ۵۰۰ مگابایت باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        extension = os.path.splitext(audio_file.name or '')[1].lower()
        if extension not in {'.mp3','.wav'}:
            return Response({'audio_file': ['فقط فایل MP3 یا WAV پشتیبانی می‌شود.']}, status=status.HTTP_400_BAD_REQUEST)
        active_track_count = release.release_tracks.exclude(song__status=Song.STATUS_DELETED).count()
        if release.release_type == ArtistRelease.TYPE_SINGLE and active_track_count >= 1:
            return Response({'detail': 'تک‌آهنگ فقط می‌تواند یک ترک داشته باشد.'}, status=status.HTTP_409_CONFLICT)
        if release.release_type == ArtistRelease.TYPE_EP and active_track_count >= 7:
            return Response({'detail': 'مینی‌آلبوم می‌تواند حداکثر ۷ ترک داشته باشد.'}, status=status.HTTP_400_BAD_REQUEST)
        if active_track_count >= 100:
            return Response({'detail': 'حداکثر ۱۰۰ ترک برای هر انتشار مجاز است.'}, status=status.HTTP_400_BAD_REQUEST)

        uploaded_urls = []
        try:
            duration, bitrate, detected_format = get_audio_info(audio_file)
            if hasattr(audio_file, 'seek'):
                audio_file.seek(0)
            original_format = (detected_format or extension.lstrip('.')).lower()
            artist_label = release.artist.artistic_name or release.artist.name
            safe_base = make_safe_filename(f'{artist_label} - {title_en or title}')
            audio_url, _ = upload_file_to_r2(audio_file, folder='songs', custom_filename=f'{safe_base}.{original_format}')
            uploaded_urls.append(audio_url)
            converted_url = None
            if original_format != 'mp3' or bitrate is None or bitrate > 128:
                try:
                    if hasattr(audio_file, 'seek'):
                        audio_file.seek(0)
                    converted_file = convert_to_128kbps(audio_file)
                    converted_url, _ = upload_file_to_r2(converted_file, folder='songs/128', custom_filename=f'{safe_base}_128.mp3')
                    uploaded_urls.append(converted_url)
                except Exception:
                    logger.exception('Admin release 128kbps conversion failed release=%s file=%s', pk, getattr(audio_file, 'name', ''))

            with transaction.atomic():
                locked = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk)
                _require_employee_admin_release(request.user, locked, 'release_add.edit')
                if locked.status != ArtistRelease.STATUS_DRAFT:
                    raise ValueError('release_not_draft')
                conflict = _lock_version_error(locked, request.data)
                if conflict:
                    transaction.on_commit(lambda values=tuple(uploaded_urls): cleanup_r2_urls(values))
                    return conflict
                locked_count = locked.release_tracks.exclude(song__status=Song.STATUS_DELETED).count()
                if locked.release_type == ArtistRelease.TYPE_SINGLE and locked_count >= 1:
                    transaction.on_commit(lambda values=tuple(uploaded_urls): cleanup_r2_urls(values))
                    return Response({'detail': 'تک‌آهنگ فقط می‌تواند یک ترک داشته باشد.'}, status=status.HTTP_409_CONFLICT)
                if locked.release_type == ArtistRelease.TYPE_EP and locked_count >= 7:
                    transaction.on_commit(lambda values=tuple(uploaded_urls): cleanup_r2_urls(values))
                    return Response({'detail': 'مینی‌آلبوم می‌تواند حداکثر ۷ ترک داشته باشد.'}, status=status.HTTP_400_BAD_REQUEST)
                if locked_count >= 100:
                    transaction.on_commit(lambda values=tuple(uploaded_urls): cleanup_r2_urls(values))
                    return Response({'detail': 'حداکثر ۱۰۰ ترک برای هر انتشار مجاز است.'}, status=status.HTTP_400_BAD_REQUEST)
                position = locked.release_tracks.count() + 1
                song = Song.objects.create(
                    title=title, title_en=title_en, artist=locked.artist,
                    audio_file=audio_url, converted_audio_url=converted_url,
                    original_format=original_format, duration_seconds=duration,
                    status=Song.STATUS_DRAFT, uploader=request.user,
                    language=str(request.data.get('language') or 'fa').strip() or 'fa',
                    is_single=False, album_disc_number=1, album_track_number=position,
                )
                ArtistReleaseTrack.objects.create(release=locked, song=song, position=position, extras={})
                sync_release_tracks(locked)
                _touch_release(locked)
            return Response(serialize_release(release_queryset().get(pk=pk), request, include_history=True), status=status.HTTP_201_CREATED)
        except ValueError as exc:
            cleanup_r2_urls(uploaded_urls)
            if str(exc) == 'release_not_draft':
                return Response({'detail': 'انتشار در حین بارگذاری از حالت پیش‌نویس خارج شد؛ فایل‌های بارگذاری‌شده پاک شدند.'}, status=status.HTTP_409_CONFLICT)
            raise
        except MediaPipelineError as exc:
            cleanup_r2_urls(uploaded_urls)
            return Response({'detail': str(exc), 'code': exc.code}, status=exc.status_code)
        except Exception:
            cleanup_r2_urls(uploaded_urls)
            logger.exception('Admin release track upload failed release=%s', pk)
            return Response({'detail': 'بارگذاری ترک کامل نشد و فایل‌های نیمه‌کاره پاک شدند.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema(tags=['Admin Releases'])
class AdminReleaseArtworkView(APIView):
    permission_classes = [IsAdminPanelUser]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, pk):
        release_scope = get_object_or_404(ArtistRelease.objects.only('pk', 'status'), pk=pk)
        _require_employee_admin_release(request.user, release_scope, 'release_add.edit')
        if release_scope.status != ArtistRelease.STATUS_DRAFT:
            return Response({'detail': 'تغییر کاور در این صفحه فقط برای پیش‌نویس مجاز است.'}, status=status.HTTP_409_CONFLICT)
        image_file = request.FILES.get('cover_image')
        if not image_file:
            return Response({'cover_image': ['انتخاب تصویر کاور الزامی است.']}, status=status.HTTP_400_BAD_REQUEST)
        if image_file.size <= 0 or image_file.size > 10 * 1024 * 1024:
            return Response({'cover_image': ['حجم تصویر کاور باید بیشتر از صفر و کمتر از ۱۰ مگابایت باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        if getattr(image_file, 'content_type', '') not in {'image/jpeg','image/png','image/webp'}:
            return Response({'cover_image': ['فرمت تصویر باید JPG، PNG یا WEBP باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        try:
            image_file.seek(0)
            with Image.open(image_file) as image:
                width, height = image.size
            image_file.seek(0)
        except Exception:
            return Response({'cover_image': ['تصویر قابل خواندن نیست.']}, status=status.HTTP_400_BAD_REQUEST)
        if width != height:
            return Response({'cover_image': ['کاور انتشار باید مربعی باشد.']}, status=status.HTTP_400_BAD_REQUEST)
        try:
            url, _ = upload_file_to_r2(image_file, folder='covers/releases')
        except MediaPipelineError as exc:
            return Response({'detail': str(exc), 'code': exc.code}, status=exc.status_code)
        old_url = ''
        with transaction.atomic():
            release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk)
            _require_employee_admin_release(request.user, release, 'release_add.edit')
            if release.status != ArtistRelease.STATUS_DRAFT:
                cleanup_r2_urls([url])
                return Response({'detail': 'تغییر کاور در این صفحه فقط برای پیش‌نویس مجاز است.'}, status=status.HTTP_409_CONFLICT)
            conflict = _lock_version_error(release, request.data)
            if conflict:
                cleanup_r2_urls([url])
                return conflict
            metadata = merged_release_metadata(release.release_metadata, release.artist_id)
            old_url = str(metadata.get('cover_url') or '')
            metadata['cover_url'] = url
            release.release_metadata = metadata
            release.validation_snapshot = {}
            release.lock_version += 1
            release.save(update_fields=['release_metadata','validation_snapshot','lock_version','updated_at'])
            sync_release_artwork(release, previous_cover=old_url)
        if old_url and old_url != url:
            transaction.on_commit(lambda old=old_url: _cleanup_unreferenced_release_media([old]))
        return Response(serialize_release(release_queryset().get(pk=pk), request, include_history=True))


@extend_schema(tags=['Admin Releases'])
class AdminReleaseValidateView(APIView):
    permission_classes = [IsAdminPanelUser]

    def post(self, request, pk):
        with transaction.atomic():
            locked = get_object_or_404(ArtistRelease.objects.select_for_update().only('pk'), pk=pk)
            release = release_queryset().get(pk=locked.pk)
            if is_employee(request.user):
                if release.status == ArtistRelease.STATUS_DRAFT:
                    _require_employee_admin_release(request.user, release, 'release_add.edit')
                elif not (has_employee_permission(request.user, 'releases.review') or has_employee_permission(request.user, 'releases.publish')):
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied('شما اجازه اعتبارسنجی این انتشار را ندارید.')
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            sync_release_tracks(release)
            release = release_queryset().get(pk=release.pk)
            validation = validation_payload(release)
            release.validation_snapshot = validation
            release.save(update_fields=['validation_snapshot','updated_at'])
            response_payload = dict(validation)
            response_payload['lock_version'] = release.lock_version
        return Response(response_payload)

@extend_schema(tags=['Admin Releases'])
class AdminReleaseActionView(APIView):
    permission_classes = [IsAdminPanelUser]

    def post(self, request, pk):
        action = str(request.data.get('action') or '').strip()
        note = str(request.data.get('note') or '').strip()
        allowed = {
            'request_changes', 'reject', 'approve', 'schedule', 'publish',
            'take_down', 'reopen', 'return_to_review',
        }
        if action not in allowed:
            return Response({'action': ['عملیات انتخاب‌شده معتبر نیست.']}, status=status.HTTP_400_BAD_REQUEST)

        transitions = {
            'request_changes': {ArtistRelease.STATUS_IN_REVIEW},
            'reject': {ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_CHANGES_REQUESTED},
            'approve': {ArtistRelease.STATUS_IN_REVIEW},
            'schedule': {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_APPROVED},
            'publish': {ArtistRelease.STATUS_DRAFT, ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_APPROVED, ArtistRelease.STATUS_SCHEDULED, ArtistRelease.STATUS_TAKEN_DOWN},
            'take_down': {ArtistRelease.STATUS_LIVE},
            'reopen': {ArtistRelease.STATUS_CHANGES_REQUESTED, ArtistRelease.STATUS_REJECTED, ArtistRelease.STATUS_TAKEN_DOWN},
            'return_to_review': {ArtistRelease.STATUS_CHANGES_REQUESTED, ArtistRelease.STATUS_REJECTED, ArtistRelease.STATUS_APPROVED},
        }

        with transaction.atomic():
            locked_release = get_object_or_404(ArtistRelease.objects.select_for_update().only('pk'), pk=pk)
            release = release_queryset().get(pk=locked_release.pk)
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            if release.status not in transitions[action]:
                return Response({
                    'detail': f"انجام این عملیات در وضعیت فعلی انتشار مجاز نیست."
                }, status=status.HTTP_409_CONFLICT)

            if is_employee(request.user):
                # The same action endpoint handles both admin-authored drafts and
                # artist review submissions; enforce the responsibility after the
                # actual release state is loaded, not only by the button shown.
                if action in {'publish', 'schedule'}:
                    if release.status == ArtistRelease.STATUS_DRAFT:
                        _require_employee_admin_release(request.user, release, 'release_add.publish')
                    else:
                        require_employee_permission(request.user, 'releases.publish')
                elif action == 'reopen':
                    require_employee_permission(
                        request.user,
                        'releases.takedown' if release.status == ArtistRelease.STATUS_TAKEN_DOWN else 'releases.review',
                    )

            removal = release_removal_state(release)
            if removal['artist_deleted'] and action in {'publish', 'schedule', 'reopen', 'return_to_review'}:
                return Response({
                    'detail': 'این انتشار توسط هنرمند حذف شده است و از پنل مدیریت قابل بازگردانی یا انتشار مجدد نیست.',
                    'code': 'artist_deleted_release',
                }, status=status.HTTP_409_CONFLICT)
            if release.status == ArtistRelease.STATUS_TAKEN_DOWN and action in {'publish', 'reopen'} and not removal['can_restore']:
                return Response({
                    'detail': 'این انتشار رکورد فعال و سالمی برای بازگردانی ندارد. در صورت نیاز از حذف دائمی استفاده کنید.',
                    'code': 'release_not_restorable',
                }, status=status.HTTP_409_CONFLICT)

            requested_scheduled_at = None
            if action == 'schedule':
                raw_scheduled_at = str(request.data.get('scheduled_at') or '').strip()
                if raw_scheduled_at:
                    requested_scheduled_at = parse_datetime(raw_scheduled_at)
                    if not requested_scheduled_at:
                        return Response(
                            {'scheduled_at': ['تاریخ و ساعت انتخاب‌شده معتبر نیست.']},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    if timezone.is_naive(requested_scheduled_at):
                        requested_scheduled_at = timezone.make_aware(requested_scheduled_at, timezone.get_current_timezone())
                    if requested_scheduled_at <= timezone.now():
                        return Response(
                            {'scheduled_at': ['زمان انتشار باید در آینده باشد.']},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    # Exact admin scheduling is authoritative. Keep the catalog
                    # release date synchronized with the actual publication day.
                    metadata = merged_release_metadata(release.release_metadata, release.artist_id)
                    scheduled_day = timezone.localtime(requested_scheduled_at, timezone.get_current_timezone()).date().isoformat()
                    if metadata.get('release_date') != scheduled_day:
                        metadata['release_date'] = scheduled_day
                        release.release_metadata = metadata
                        release.validation_snapshot = {}
                        release.save(update_fields=['release_metadata', 'validation_snapshot', 'updated_at'])
            elif action == 'publish':
                # Publishing immediately must not rewrite catalog metadata between
                # the explicit validation request and the final action. ``published_at``
                # is the authoritative instant the release became live; release_date
                # remains the metadata date the admin already reviewed and validated.
                pass

            if action in {'approve', 'schedule', 'publish'}:
                sync_release_tracks(release)
                release = release_queryset().get(pk=release.pk)
                validation = validation_payload(release)
                release.validation_snapshot = validation
                release.save(update_fields=['validation_snapshot', 'updated_at'])
                if not validation['valid']:
                    return Response({'detail': 'اعتبارسنجی انتشار انجام نشد. خطاهای مشخص‌شده را اصلاح کنید.', 'validation': validation}, status=status.HTTP_400_BAD_REQUEST)

            if action == 'request_changes':
                change_status(release, ArtistRelease.STATUS_CHANGES_REQUESTED, actor=request.user, note=note or 'مدیر درخواست اصلاحات ثبت کرد.')
            elif action == 'reject':
                Song.objects.filter(release_track_links__release=release).exclude(
                    status=Song.STATUS_DELETED
                ).update(status=Song.STATUS_REJECTED)
                change_status(release, ArtistRelease.STATUS_REJECTED, actor=request.user, note=note or 'انتشار توسط مدیر رد شد.')
            elif action == 'approve':
                release = approve_release(release, actor=request.user, note=note)
            elif action == 'schedule':
                # Older clients may omit an exact time and continue to use the
                # release-date-at-midnight behavior; new admin clients send ISO.
                scheduled_at = requested_scheduled_at or scheduled_datetime(release)
                if not scheduled_at or scheduled_at <= timezone.now():
                    return Response(
                        {'scheduled_at': ['زمان انتشار باید در آینده باشد.']},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                release = prepare_release(release, schedule=False)
                release.scheduled_at = scheduled_at
                release.save(update_fields=['scheduled_at', 'updated_at'])
                change_status(release, ArtistRelease.STATUS_SCHEDULED, actor=request.user, note=note or 'انتشار زمان‌بندی شد.')
            elif action == 'publish':
                release = materialize_release(release, publish=True)
                change_status(release, ArtistRelease.STATUS_LIVE, actor=request.user, note=note or 'انتشار منتشر شد.')
            elif action == 'take_down':
                take_down_release(release)
                release = ArtistRelease.objects.select_for_update().get(pk=release.pk)
                change_status(release, ArtistRelease.STATUS_TAKEN_DOWN, actor=request.user, note=note or 'انتشار از دسترس خارج شد.')
            elif action == 'reopen':
                Song.objects.filter(release_track_links__release=release).exclude(
                    status=Song.STATUS_DELETED
                ).update(status=Song.STATUS_DRAFT)
                release.submitted_at = None
                release.scheduled_at = None
                release.validation_snapshot = {}
                release.save(update_fields=['submitted_at', 'scheduled_at', 'validation_snapshot', 'updated_at'])
                change_status(release, ArtistRelease.STATUS_DRAFT, actor=request.user, note=note or 'انتشار برای ویرایش دوباره باز شد.')
            elif action == 'return_to_review':
                Song.objects.filter(release_track_links__release=release).exclude(
                    status=Song.STATUS_DELETED
                ).update(status=Song.STATUS_PENDING)
                release.submitted_at = release.submitted_at or timezone.now()
                release.save(update_fields=['submitted_at', 'updated_at'])
                change_status(release, ArtistRelease.STATUS_IN_REVIEW, actor=request.user, note=note or 'انتشار دوباره به صف بررسی برگشت.')

        if release.status == ArtistRelease.STATUS_DRAFT:
            return Response({'id': str(release.pk), 'removed_from_admin_queue': True})
        return Response(serialize_release(release_queryset().get(pk=release.pk), request, include_history=True))

