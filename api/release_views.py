from __future__ import annotations

import json
import logging
from PIL import Image
from django.db import transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Album,
    Artist,
    ArtistRelease,
    ArtistReleaseStatusHistory,
    ArtistReleaseTrack,
    ReleaseContributor,
    Song,
    User,
)
from .release_service import (
    apply_track_metadata,
    change_status,
    create_revision,
    ensure_editable_song,
    materialize_release,
    merged_release_metadata,
    merged_shared,
    normalize_track_extras,
    prepare_release,
    publish_due_releases,
    release_queryset,
    scheduled_datetime,
    serialize_release,
    snapshot_song,
    sync_release_tracks,
    take_down_release,
    validation_payload,
)
from .utils import MediaPipelineError, cleanup_r2_urls, upload_file_to_r2

logger = logging.getLogger(__name__)


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
        return Response({'lock_version': ['Provide a valid lock version.']}, status=status.HTTP_400_BAD_REQUEST)
    if expected != release.lock_version:
        return Response({
            'detail': 'This release changed in another tab or session. Reload before saving again.',
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
    if release.status != ArtistRelease.STATUS_DRAFT:
        return Response(
            {'detail': 'This release is locked. Create an editable revision to make changes.'},
            status=status.HTTP_409_CONFLICT,
        )
    return None


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
    if Song.STATUS_PUBLISHED in statuses:
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
        'current_step': 6,
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
        'current_step': 6,
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
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
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
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
        release_type = str(request.data.get('release_type') or ArtistRelease.TYPE_ALBUM)
        if release_type not in dict(ArtistRelease.TYPE_CHOICES):
            return Response({'release_type': ['Choose a valid release type.']}, status=status.HTTP_400_BAD_REQUEST)
        title = str(request.data.get('title') or 'Untitled Release').strip() or 'Untitled Release'
        title_en = str(request.data.get('title_en') or '').strip()
        if len(title) > 400 or len(title_en) > 400:
            return Response({'title': ['Release titles must be 400 characters or fewer.']}, status=status.HTTP_400_BAD_REQUEST)
        incoming_release_metadata = request.data.get('release_metadata', {})
        incoming_shared_metadata = request.data.get('shared_metadata', {})
        if not isinstance(incoming_release_metadata, dict):
            return Response({'release_metadata': ['Provide an object.']}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(incoming_shared_metadata, dict):
            return Response({'shared_metadata': ['Provide an object.']}, status=status.HTTP_400_BAD_REQUEST)
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
            note='Release workspace created.',
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
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_release(release, request, include_history=True))

    def patch(self, request, pk):
        release = self._get(request, pk)
        if not release:
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            release = ArtistRelease.objects.select_for_update().get(pk=release.pk, artist_id=release.artist_id)
            locked = _draft_or_409(release)
            if locked:
                return locked
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            allowed = {'title', 'title_en', 'release_type', 'previously_released', 'current_step'}
            for field in allowed:
                if field not in request.data:
                    continue
                value = request.data.get(field)
                if field == 'release_type' and value not in dict(ArtistRelease.TYPE_CHOICES):
                    return Response({'release_type': ['Choose a valid release type.']}, status=status.HTTP_400_BAD_REQUEST)
                if field in {'title', 'title_en'}:
                    value = str(value or '').strip()
                    if len(value) > 400:
                        return Response({field: ['Keep this title at 400 characters or fewer.']}, status=status.HTTP_400_BAD_REQUEST)
                    if field == 'title' and not value:
                        value = 'Untitled Release'
                if field == 'current_step':
                    try:
                        value = max(1, min(6, int(value)))
                    except (TypeError, ValueError):
                        value = 1
                elif field == 'previously_released':
                    if isinstance(value, str):
                        value = value.strip().lower() in {'1', 'true', 'yes', 'on'}
                    else:
                        value = bool(value)
                setattr(release, field, value)
            if 'shared_metadata' in request.data:
                incoming_shared = request.data.get('shared_metadata')
                if not isinstance(incoming_shared, dict):
                    return Response({'shared_metadata': ['Provide an object.']}, status=status.HTTP_400_BAD_REQUEST)
                shared = dict(release.shared_metadata or {})
                shared.update(incoming_shared)
                release.shared_metadata = merged_shared(shared)
            if 'release_metadata' in request.data:
                incoming_metadata = request.data.get('release_metadata')
                if not isinstance(incoming_metadata, dict):
                    return Response({'release_metadata': ['Provide an object.']}, status=status.HTTP_400_BAD_REQUEST)
                existing_cover = str((release.release_metadata or {}).get('cover_url') or '')
                metadata = dict(release.release_metadata or {})
                metadata.update(incoming_metadata)
                release.release_metadata = merged_release_metadata(metadata, release.artist_id)
                # Artist artwork must pass the dedicated image validation/upload endpoint.
                release.release_metadata['cover_url'] = existing_cover
            if 'track_extras' in request.data:
                if not isinstance(request.data.get('track_extras'), dict):
                    return Response({'track_extras': ['Provide an object keyed by song ID.']}, status=status.HTTP_400_BAD_REQUEST)
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
        return Response(serialize_release(release_queryset().get(pk=release.pk), request))

    put = patch

    def delete(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)

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
            unique_links = [link for link in links if link.song_id not in shared_song_ids]
            hard_delete_ids = []
            soft_delete_songs = []
            preserve_release_tracks = release.status in {ArtistRelease.STATUS_LIVE, ArtistRelease.STATUS_TAKEN_DOWN}
            for link in unique_links:
                song = link.song
                has_accounting = bool(song.plays) or song.play_counts.exists()
                if preserve_release_tracks or song.status in {Song.STATUS_PUBLISHED, Song.STATUS_DELETED} or has_accounting:
                    soft_delete_songs.append(song)
                else:
                    hard_delete_ids.append(song.pk)
                    media_urls.extend(filter(None, [
                        song.audio_file,
                        song.converted_audio_url,
                        song.preview_audio_url,
                        song.cover_image,
                    ]))

            raw_cover = str((release.release_metadata or {}).get('cover_url') or '')
            if raw_cover:
                media_urls.append(raw_cover)

            album = release.album
            ArtistReleaseTrack.objects.filter(release=release).delete()
            for song in soft_delete_songs:
                if song.status != Song.STATUS_DELETED:
                    song.status = Song.STATUS_DELETED
                    song.save(update_fields=['status', 'updated_at'])
            if hard_delete_ids:
                Song.objects.filter(pk__in=hard_delete_ids).delete()
            release.delete()

            if album and not album.songs.exists():
                if album.cover_image:
                    media_urls.append(album.cover_image)
                album.delete()

            transaction.on_commit(lambda values=tuple(media_urls): _cleanup_unreferenced_release_media(values))

        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseTracksView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)

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
                    return Response({'ordered_song_ids': ['The order must contain every release track exactly once.']}, status=status.HTTP_400_BAD_REQUEST)
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
                    return Response({'song_ids': ['Select at least one recording.']}, status=status.HTTP_400_BAD_REQUEST)
                songs = {song.id: song for song in Song.objects.filter(
                    id__in=song_ids, artist=artist
                ).exclude(status=Song.STATUS_DELETED).prefetch_related(
                    'featured_artists', 'genres', 'sub_genres', 'moods', 'tags'
                )}
                missing = [song_id for song_id in song_ids if song_id not in songs]
                if missing:
                    return Response({'song_ids': [f'Recordings are unavailable or not owned by this artist: {missing}']}, status=status.HTTP_400_BAD_REQUEST)

                existing_links = list(ArtistReleaseTrack.objects.select_for_update().filter(release=release))
                existing_ids = {item.song_id for item in existing_links}
                existing_source_ids = {item.source_song_id for item in existing_links if item.source_song_id}
                candidates = [song_id for song_id in song_ids if song_id not in existing_ids and song_id not in existing_source_ids]
                if len(existing_links) + len(candidates) > 100:
                    return Response({'song_ids': ['A release cannot contain more than 100 tracks.']}, status=status.HTTP_400_BAD_REQUEST)
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
                if candidates:
                    sync_release_tracks(release)
                    _touch_release(release)
            else:
                return Response({'action': ['Choose add or reorder.']}, status=status.HTTP_400_BAD_REQUEST)

        return Response(serialize_release(release_queryset().get(pk=release.pk), request))

    def delete(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
        song_ids = _id_list(request.data.get('song_ids'))
        if not song_ids:
            return Response({'song_ids': ['Select at least one release track.']}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk, artist=artist)
            locked = _draft_or_409(release)
            if locked:
                return locked
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            removed_ids = list(
                ArtistReleaseTrack.objects.select_for_update()
                .filter(release=release, song_id__in=song_ids)
                .values_list('song_id', flat=True)
            )
            ArtistReleaseTrack.objects.filter(release=release, song_id__in=removed_ids).delete()
            if removed_ids:
                Song.objects.filter(id__in=removed_ids).exclude(release_track_links__isnull=False).update(status=Song.STATUS_DRAFT)
            links = list(ArtistReleaseTrack.objects.select_for_update().filter(release=release).order_by('position', 'id'))
            for index, link in enumerate(links, start=1):
                if link.position != index:
                    ArtistReleaseTrack.objects.filter(pk=link.pk).update(position=1000 + index)
            for index, link in enumerate(links, start=1):
                ArtistReleaseTrack.objects.filter(pk=link.pk).update(position=index)
            sync_release_tracks(release)
            _touch_release(release)

        return Response(serialize_release(release_queryset().get(pk=release.pk), request))


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseBulkMetadataView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
        metadata = request.data.get('metadata')
        song_ids = _id_list(request.data.get('song_ids'))
        if not isinstance(metadata, dict) or not song_ids:
            return Response(
                {'detail': 'Provide handwritten metadata and at least one release track.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if 'rows' in request.data or 'copy_from_song_id' in request.data:
            return Response(
                {'detail': 'File import and metadata copying are no longer supported. Enter metadata manually.'},
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
                return Response({'detail': 'No matching release tracks were updated.'}, status=status.HTTP_400_BAD_REQUEST)
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
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
        locked = _draft_or_409(release)
        if locked:
            return locked
        conflict = _lock_version_error(release, request.data)
        if conflict:
            return conflict
        image_file = request.FILES.get('cover_image')
        if not image_file:
            return Response({'cover_image': ['Artwork is required.']}, status=status.HTTP_400_BAD_REQUEST)
        if image_file.size > 10 * 1024 * 1024:
            return Response({'cover_image': ['Artwork must be smaller than 10MB.']}, status=status.HTTP_400_BAD_REQUEST)
        if getattr(image_file, 'content_type', '') not in {'image/jpeg', 'image/png', 'image/webp'}:
            return Response({'cover_image': ['Artwork must be JPG, PNG, or WEBP.']}, status=status.HTTP_400_BAD_REQUEST)
        try:
            image_file.seek(0)
            with Image.open(image_file) as image:
                width, height = image.size
            image_file.seek(0)
        except Exception:
            return Response({'cover_image': ['Artwork is damaged or unreadable.']}, status=status.HTTP_400_BAD_REQUEST)
        if width != height:
            return Response({'cover_image': ['Artwork must be square.']}, status=status.HTTP_400_BAD_REQUEST)
        try:
            url, _ = upload_file_to_r2(image_file, folder='covers/releases')
        except MediaPipelineError as exc:
            return Response({'cover_image': [str(exc)], 'code': exc.code}, status=exc.status_code)

        old_url = ''
        try:
            with transaction.atomic():
                release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=release.pk, artist=artist)
                locked = _draft_or_409(release)
                if locked:
                    cleanup_r2_urls([url])
                    return locked
                conflict = _lock_version_error(release, request.data)
                if conflict:
                    cleanup_r2_urls([url])
                    return conflict
                metadata = merged_release_metadata(release.release_metadata, release.artist_id)
                old_url = str(metadata.get('cover_url') or '')
                metadata['cover_url'] = url
                release.release_metadata = metadata
                release.lock_version += 1
                release.validation_snapshot = {}
                release.save(update_fields=['release_metadata', 'lock_version', 'validation_snapshot', 'updated_at'])
        except Exception:
            cleanup_r2_urls([url])
            logger.exception('Release artwork save failed release=%s user=%s', pk, request.user.pk)
            return Response(
                {'detail': 'Artwork uploaded but could not be attached to the release.', 'code': 'artwork_save_failed'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        if old_url and old_url != url and not ArtistRelease.objects.filter(release_metadata__cover_url=old_url).exists():
            cleanup_r2_urls([old_url])
        return Response(serialize_release(release_queryset().get(pk=release.pk), request))


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseValidateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
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
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            locked_release = get_object_or_404(ArtistRelease.objects.select_for_update().only('pk'), pk=pk, artist=artist)
            release = release_queryset().get(pk=locked_release.pk)
            locked = _draft_or_409(release)
            if locked:
                return locked
            conflict = _lock_version_error(release, request.data)
            if conflict:
                return conflict
            sync_release_tracks(release)
            release = release_queryset().get(pk=release.pk)
            validation = validation_payload(release)
            release.validation_snapshot = validation
            release.save(update_fields=['validation_snapshot', 'updated_at'])
            if not validation['valid']:
                return Response({'detail': 'Release validation failed.', 'validation': validation}, status=status.HTTP_400_BAD_REQUEST)
            links = list(ArtistReleaseTrack.objects.select_for_update().select_related('song').filter(release=release).order_by('position', 'id'))
            for link in links:
                link.metadata_snapshot = snapshot_song(link.song)
                link.save(update_fields=['metadata_snapshot', 'updated_at'])
                Song.objects.filter(pk=link.song_id).exclude(status=Song.STATUS_DELETED).update(
                    status=Song.STATUS_PENDING,
                    is_single=release.release_type == ArtistRelease.TYPE_SINGLE,
                )
            release.submitted_at = timezone.now()
            release.current_step = 6
            release.save(update_fields=['submitted_at', 'current_step', 'updated_at'])
            change_status(release, ArtistRelease.STATUS_IN_REVIEW, actor=request.user, note='Submitted by artist for review.')
        return Response(serialize_release(release_queryset().get(pk=release.pk), request))


@extend_schema(tags=['Artist Releases'])
class ArtistReleaseCloneView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        artist = _artist_for_user(request.user)
        release = get_object_or_404(release_queryset(), pk=pk, artist=artist) if artist else None
        if not release:
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
        mode = str(request.data.get('mode') or 'duplicate')
        if mode not in {'duplicate', 'revision'}:
            return Response({'mode': ['Choose duplicate or revision.']}, status=status.HTTP_400_BAD_REQUEST)
        copy = create_revision(release, uploader=request.user, mode=mode)
        return Response(serialize_release(copy, request), status=status.HTTP_201_CREATED)


@extend_schema(tags=['Artist Releases'])
class ArtistContributorListCreateView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
        rows = ReleaseContributor.objects.filter(artist=artist)
        return Response({'count': rows.count(), 'results': [
            {'id': item.id, 'name': item.name, 'name_en': item.name_en, 'roles': item.roles, 'created_at': item.created_at, 'updated_at': item.updated_at}
            for item in rows
        ]})

    def post(self, request):
        artist = _artist_for_user(request.user)
        if not artist:
            return Response({'detail': 'Artist profile not found.'}, status=status.HTTP_404_NOT_FOUND)
        name = str(request.data.get('name') or '').strip()
        if not name:
            return Response({'name': ['Contributor name is required.']}, status=status.HTTP_400_BAD_REQUEST)
        if len(name) > 255:
            return Response({'name': ['Contributor name must be 255 characters or fewer.']}, status=status.HTTP_400_BAD_REQUEST)
        name_en = str(request.data.get('name_en') or '').strip()
        if len(name_en) > 255:
            return Response({'name_en': ['English contributor name must be 255 characters or fewer.']}, status=status.HTTP_400_BAD_REQUEST)
        incoming_roles = request.data.get('roles', [])
        if not isinstance(incoming_roles, list):
            return Response({'roles': ['Provide a list of contributor roles.']}, status=status.HTTP_400_BAD_REQUEST)
        allowed_roles = {'producer', 'composer', 'lyricist', 'songwriter', 'performer', 'engineer', 'remixer', 'other'}
        roles = []
        for value in incoming_roles:
            role = str(value or '').strip().lower()
            if role and role not in roles:
                roles.append(role)
        invalid_roles = [role for role in roles if role not in allowed_roles]
        if invalid_roles:
            return Response({'roles': [f'Unsupported contributor roles: {invalid_roles}']}, status=status.HTTP_400_BAD_REQUEST)
        if len(roles) > 10:
            return Response({'roles': ['A contributor cannot have more than 10 roles.']}, status=status.HTTP_400_BAD_REQUEST)
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
    permission_classes = [permissions.IsAdminUser]

    def get(self, request):
        publish_due_releases()
        queryset = release_queryset().all()
        query = str(request.query_params.get('q') or '').strip()
        status_filter = str(request.query_params.get('status') or '').strip()
        artist_id = request.query_params.get('artist_id')
        if query:
            queryset = queryset.filter(Q(title__icontains=query) | Q(title_en__icontains=query) | Q(artist__name__icontains=query) | Q(artist__artistic_name__icontains=query))
        if status_filter:
            queryset = queryset.filter(status__in=[item.strip() for item in status_filter.split(',') if item.strip()])
        if artist_id:
            queryset = queryset.filter(artist_id=artist_id)
        try:
            page = max(1, int(request.query_params.get('page', 1)))
            page_size = max(1, min(100, int(request.query_params.get('page_size', 20))))
        except (TypeError, ValueError):
            page, page_size = 1, 20
        count = queryset.count()
        start = (page - 1) * page_size
        rows = queryset[start:start + page_size]
        return Response({'count': count, 'page': page, 'page_size': page_size, 'results': [serialize_release(item, request) for item in rows]})


@extend_schema(tags=['Admin Releases'])
class AdminReleaseDetailView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def get(self, request, pk):
        release = get_object_or_404(release_queryset(), pk=pk)
        return Response(serialize_release(release, request, include_history=True))

    def patch(self, request, pk):
        with transaction.atomic():
            release = get_object_or_404(ArtistRelease.objects.select_for_update(), pk=pk)
            changed_fields = []
            if 'admin_note' in request.data:
                release.admin_note = str(request.data.get('admin_note') or '')
                changed_fields.append('admin_note')
            if 'review_note' in request.data:
                release.review_note = str(request.data.get('review_note') or '')
                changed_fields.append('review_note')
            if 'release_metadata' in request.data:
                if release.status not in {
                    ArtistRelease.STATUS_IN_REVIEW,
                    ArtistRelease.STATUS_CHANGES_REQUESTED,
                    ArtistRelease.STATUS_APPROVED,
                }:
                    return Response(
                        {'detail': 'Release metadata is frozen in this status. Reopen or create a revision first.'},
                        status=status.HTTP_409_CONFLICT,
                    )
                incoming = request.data.get('release_metadata')
                if not isinstance(incoming, dict):
                    return Response({'release_metadata': ['Provide an object.']}, status=status.HTTP_400_BAD_REQUEST)
                metadata = dict(release.release_metadata or {})
                metadata.update(incoming)
                release.release_metadata = merged_release_metadata(metadata, release.artist_id)
                release.validation_snapshot = {}
                changed_fields.extend(['release_metadata', 'validation_snapshot'])
            if changed_fields:
                release.lock_version += 1
                changed_fields.extend(['lock_version', 'updated_at'])
                release.save(update_fields=list(dict.fromkeys(changed_fields)))
        return Response(serialize_release(release_queryset().get(pk=release.pk), request, include_history=True))


@extend_schema(tags=['Admin Releases'])
class AdminReleaseActionView(APIView):
    permission_classes = [permissions.IsAdminUser]

    def post(self, request, pk):
        action = str(request.data.get('action') or '').strip()
        note = str(request.data.get('note') or '').strip()
        allowed = {
            'request_changes', 'reject', 'approve', 'schedule', 'publish',
            'take_down', 'reopen', 'return_to_review',
        }
        if action not in allowed:
            return Response({'action': [f'Choose one of: {sorted(allowed)}']}, status=status.HTTP_400_BAD_REQUEST)

        transitions = {
            'request_changes': {ArtistRelease.STATUS_IN_REVIEW},
            'reject': {ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_CHANGES_REQUESTED},
            'approve': {ArtistRelease.STATUS_IN_REVIEW},
            'schedule': {ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_APPROVED},
            'publish': {ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_APPROVED, ArtistRelease.STATUS_SCHEDULED, ArtistRelease.STATUS_TAKEN_DOWN},
            'take_down': {ArtistRelease.STATUS_LIVE},
            'reopen': {ArtistRelease.STATUS_CHANGES_REQUESTED, ArtistRelease.STATUS_REJECTED, ArtistRelease.STATUS_TAKEN_DOWN},
            'return_to_review': {ArtistRelease.STATUS_CHANGES_REQUESTED, ArtistRelease.STATUS_REJECTED, ArtistRelease.STATUS_APPROVED},
        }

        with transaction.atomic():
            locked_release = get_object_or_404(ArtistRelease.objects.select_for_update().only('pk'), pk=pk)
            release = release_queryset().get(pk=locked_release.pk)
            if release.status not in transitions[action]:
                return Response({
                    'detail': f"Action '{action}' is not allowed while the release is '{release.status}'."
                }, status=status.HTTP_409_CONFLICT)

            if action in {'approve', 'schedule', 'publish'}:
                sync_release_tracks(release)
                release = release_queryset().get(pk=release.pk)
                validation = validation_payload(release)
                release.validation_snapshot = validation
                release.save(update_fields=['validation_snapshot', 'updated_at'])
                if not validation['valid']:
                    return Response({'detail': 'Release validation failed.', 'validation': validation}, status=status.HTTP_400_BAD_REQUEST)

            if action == 'request_changes':
                change_status(release, ArtistRelease.STATUS_CHANGES_REQUESTED, actor=request.user, note=note or 'Changes requested by admin.')
            elif action == 'reject':
                Song.objects.filter(release_track_links__release=release).exclude(
                    status=Song.STATUS_DELETED
                ).update(status=Song.STATUS_REJECTED)
                change_status(release, ArtistRelease.STATUS_REJECTED, actor=request.user, note=note or 'Release rejected by admin.')
            elif action == 'approve':
                release = prepare_release(release, schedule=False)
                change_status(release, ArtistRelease.STATUS_APPROVED, actor=request.user, note=note or 'Release approved.')
            elif action == 'schedule':
                scheduled_at = scheduled_datetime(release)
                if not scheduled_at or scheduled_at <= timezone.now():
                    return Response(
                        {'release_date': ['Scheduling requires a future release date. Publish directly for today or a past catalog date.']},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                release = prepare_release(release, schedule=True)
                change_status(release, ArtistRelease.STATUS_SCHEDULED, actor=request.user, note=note or 'Release scheduled.')
            elif action == 'publish':
                release = materialize_release(release, publish=True)
                change_status(release, ArtistRelease.STATUS_LIVE, actor=request.user, note=note or 'Release published.')
            elif action == 'take_down':
                take_down_release(release)
                release = ArtistRelease.objects.select_for_update().get(pk=release.pk)
                change_status(release, ArtistRelease.STATUS_TAKEN_DOWN, actor=request.user, note=note or 'Release taken down.')
            elif action == 'reopen':
                Song.objects.filter(release_track_links__release=release).exclude(
                    status=Song.STATUS_DELETED
                ).update(status=Song.STATUS_DRAFT)
                release.submitted_at = None
                release.scheduled_at = None
                release.validation_snapshot = {}
                release.save(update_fields=['submitted_at', 'scheduled_at', 'validation_snapshot', 'updated_at'])
                change_status(release, ArtistRelease.STATUS_DRAFT, actor=request.user, note=note or 'Release reopened for editing.')
            elif action == 'return_to_review':
                Song.objects.filter(release_track_links__release=release).exclude(
                    status=Song.STATUS_DELETED
                ).update(status=Song.STATUS_PENDING)
                release.submitted_at = release.submitted_at or timezone.now()
                release.save(update_fields=['submitted_at', 'updated_at'])
                change_status(release, ArtistRelease.STATUS_IN_REVIEW, actor=request.user, note=note or 'Release returned to review.')

        return Response(serialize_release(release_queryset().get(pk=release.pk), request, include_history=True))

