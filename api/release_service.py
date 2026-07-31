from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import (
    Album,
    Artist,
    ArtistRelease,
    Genre,
    Mood,
    SubGenre,
    Tag,
    ArtistReleaseStatusHistory,
    ArtistReleaseTrack,
    Song,
)
from .serializers import SongSerializer


DEFAULT_SHARED_METADATA = {
    'language': 'fa',
    'label': '',
    'label_en': '',
    'genre_ids': [],
    'sub_genre_ids': [],
    'mood_ids': [],
    'tag_ids': [],
    'producers': [],
    'producers_en': [],
    'composers': [],
    'composers_en': [],
    'lyricists': [],
    'lyricists_en': [],
}

DEFAULT_RELEASE_METADATA = {
    'release_date': '',
    'original_release_date': '',
    'upc': '',
    'primary_genre_id': None,
    'secondary_genre_id': None,
    'label': '',
    'label_en': '',
    'p_copyright': '',
    'c_copyright': '',
    'territories': ['WORLDWIDE'],
    'platforms': ['ALL'],
    'preorder_enabled': False,
    'preorder_date': '',
    'release_artist_ids': [],
    'cover_url': '',
    'description': '',
    'description_en': '',
}

TRACK_METADATA_FIELDS = (
    'title', 'title_en', 'release_date', 'language', 'description', 'description_en',
    'lyrics', 'lyrics_en', 'tempo', 'energy', 'danceability', 'valence',
    'acousticness', 'instrumentalness', 'speechiness', 'live_performed', 'label',
    'label_en', 'producers', 'producers_en', 'composers', 'composers_en',
    'lyricists', 'lyricists_en', 'credits', 'credits_en',
)
TRACK_RELATION_FIELDS = {
    'genre_ids': 'genres',
    'sub_genre_ids': 'sub_genres',
    'mood_ids': 'moods',
    'tag_ids': 'tags',
    'featured_artist_ids': 'featured_artists',
}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return bool(value)


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ''):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def merged_shared(value: dict | None) -> dict:
    result = deepcopy(DEFAULT_SHARED_METADATA)
    if isinstance(value, dict):
        result.update(value)
    for key in ('genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids'):
        result[key] = _clean_int_list(result.get(key))
    for key in ('producers', 'producers_en', 'composers', 'composers_en', 'lyricists', 'lyricists_en'):
        result[key] = _clean_string_list(result.get(key))
    return result


def merged_release_metadata(value: dict | None, artist_id: int | None = None) -> dict:
    result = deepcopy(DEFAULT_RELEASE_METADATA)
    if isinstance(value, dict):
        result.update(value)
    result['release_artist_ids'] = _clean_int_list(result.get('release_artist_ids'))
    if artist_id and artist_id not in result['release_artist_ids']:
        result['release_artist_ids'].insert(0, artist_id)
    result['primary_genre_id'] = _optional_positive_int(result.get('primary_genre_id'))
    result['secondary_genre_id'] = _optional_positive_int(result.get('secondary_genre_id'))
    result['territories'] = _clean_string_list(result.get('territories')) or ['WORLDWIDE']
    result['platforms'] = _clean_string_list(result.get('platforms')) or ['ALL']
    result['preorder_enabled'] = _as_bool(result.get('preorder_enabled'))
    for key in (
        'release_date', 'original_release_date', 'preorder_date', 'upc', 'label', 'label_en',
        'p_copyright', 'c_copyright', 'cover_url', 'description', 'description_en',
    ):
        result[key] = str(result.get(key) or '').strip()
    result['upc'] = ''.join(character for character in result['upc'] if character.isdigit())
    if not result['preorder_enabled']:
        result['preorder_date'] = ''
    return result


def _clean_int_list(value: Any) -> list[int]:
    if value in (None, ''):
        return []
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    result = []
    for item in value:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in result:
            result.append(parsed)
    return result


def _clean_string_list(value: Any) -> list[str]:
    if value in (None, ''):
        return []
    if isinstance(value, str):
        value = value.replace('،', ',').split(',')
    if not isinstance(value, (list, tuple, set)):
        value = [value]
    result = []
    for item in value:
        text = str(item or '').strip()
        if text and text not in result:
            result.append(text)
    return result




def _valid_gtin(value: str) -> bool:
    digits = ''.join(character for character in str(value or '') if character.isdigit())
    if len(digits) not in (12, 13, 14):
        return False
    payload, supplied = digits[:-1], int(digits[-1])
    weighted = 0
    for offset, character in enumerate(reversed(payload)):
        weighted += int(character) * (3 if offset % 2 == 0 else 1)
    return (10 - weighted % 10) % 10 == supplied

def _positive_int(value: Any, default: int = 1, maximum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    parsed = max(1, parsed)
    return min(parsed, maximum) if maximum else parsed


def normalize_track_extras(value: dict | None, default_position: int = 1) -> dict:
    result = deepcopy(value) if isinstance(value, dict) else {}
    result['disc_number'] = _positive_int(result.get('disc_number'), 1, 99)
    result['track_number'] = _positive_int(result.get('track_number'), default_position, 999)
    try:
        result['preview_start'] = max(0, int(result.get('preview_start') or 0))
    except (TypeError, ValueError):
        result['preview_start'] = 0
    result['explicit'] = _as_bool(result.get('explicit', False))
    result['isrc'] = re.sub(r'[-\s]', '', str(result.get('isrc') or '')).upper()
    result['version'] = str(result.get('version') or '').strip()
    result['master_owner'] = str(result.get('master_owner') or '').strip()
    result['publishing_owner'] = str(result.get('publishing_owner') or '').strip()
    result['rights_notes'] = str(result.get('rights_notes') or '').strip()
    result['platform_settings'] = result.get('platform_settings') if isinstance(result.get('platform_settings'), dict) else {}
    result['licenses'] = _clean_string_list(result.get('licenses'))
    splits = []
    for item in result.get('songwriter_splits') or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        share = item.get('share', 0)
        if name or str(share).strip():
            splits.append({'name': name, 'share': share})
    result['songwriter_splits'] = splits
    return result

def artist_payload(artist: Artist | None) -> dict | None:
    if not artist:
        return None
    return {
        'id': artist.id,
        'name': artist.artistic_name or artist.name,
        'name_en': artist.artistic_name_en or artist.name_en or artist.artistic_name or artist.name,
        'artistic_name': artist.artistic_name,
        'artistic_name_en': artist.artistic_name_en,
        'profile_image': artist.profile_image or '',
    }


def release_queryset():
    track_qs = ArtistReleaseTrack.objects.select_related('song', 'source_song').prefetch_related(
        Prefetch('song__featured_artists'), Prefetch('song__genres'), Prefetch('song__sub_genres'),
        Prefetch('song__moods'), Prefetch('song__tags'),
    ).order_by('position', 'id')
    return ArtistRelease.objects.select_related('artist', 'album', 'source_release').prefetch_related(
        Prefetch('release_tracks', queryset=track_qs), 'status_history'
    )


def _track_missing(song: Song, extras: dict) -> list[str]:
    missing = []
    if not str(song.title or '').strip():
        missing.append('title')
    if not str(song.audio_file or '').strip():
        missing.append('audio')
    if not str(song.language or '').strip():
        missing.append('language')
    if not song.genres.exists():
        missing.append('genre')
    if not (song.composers or []):
        missing.append('composer')
    if not (song.lyricists or []) and not bool(song.instrumentalness and song.instrumentalness >= 80):
        missing.append('lyricist')
    if not str(extras.get('master_owner') or '').strip():
        missing.append('master owner')
    if not str(extras.get('publishing_owner') or '').strip():
        missing.append('publishing owner')
    return missing


def _track_completion(song: Song, extras: dict) -> tuple[int, list[str]]:
    missing = _track_missing(song, extras)
    total = 8
    return max(0, round((total - len(missing)) / total * 100)), missing


def serialize_track(link: ArtistReleaseTrack, request=None) -> dict:
    data = dict(SongSerializer(link.song, context={'request': request}).data)
    completion, missing = _track_completion(link.song, link.extras or {})
    data.update({
        'has_audio': bool(link.song.audio_file),
        'metadata_completion': completion,
        'missing_metadata': missing,
        'release_extras': normalize_track_extras(link.extras, link.position),
        'track_number': _positive_int((link.extras or {}).get('track_number'), link.position),
        'disc_number': _positive_int((link.extras or {}).get('disc_number'), 1, 99),
    })
    return data


def validation_payload(release: ArtistRelease) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []
    metadata = merged_release_metadata(release.release_metadata, release.artist_id)
    tracks = list(release.release_tracks.all())

    def error(section: str, message: str, track_id: int | None = None):
        item = {'section': section, 'message': message}
        if track_id:
            item['track_id'] = track_id
        errors.append(item)

    def warning(section: str, message: str, track_id: int | None = None):
        item = {'section': section, 'message': message}
        if track_id:
            item['track_id'] = track_id
        warnings.append(item)

    if not str(release.title or '').strip() or release.title.strip().lower() == 'untitled release':
        error('release', 'Enter the release title.')
    if release.release_type not in dict(ArtistRelease.TYPE_CHOICES):
        error('release', 'Choose a valid release type.')

    count = len(tracks)
    if release.release_type == ArtistRelease.TYPE_SINGLE and count != 1:
        error('tracklist', 'A single must contain exactly one track.')
    elif release.release_type == ArtistRelease.TYPE_EP and not (2 <= count <= 7):
        error('tracklist', 'An EP must contain between 2 and 7 tracks.')
    elif release.release_type in (ArtistRelease.TYPE_ALBUM, ArtistRelease.TYPE_COMPILATION) and count < 2:
        error('tracklist', 'This release type must contain at least two tracks.')
    if count > 100:
        error('tracklist', 'A release cannot contain more than 100 tracks.')

    cover_url = str(metadata.get('cover_url') or '').strip()
    if not cover_url:
        error('artwork', 'Upload square release artwork.')
    release_date = parse_date(str(metadata.get('release_date') or ''))
    if not release_date:
        error('release', 'Choose a valid release date.')
    original_date = parse_date(str(metadata.get('original_release_date') or '')) if metadata.get('original_release_date') else None
    if release.previously_released and not original_date:
        error('release', 'Enter the original release date for previously released content.')
    today = timezone.localdate()
    if release_date and not release.previously_released and release_date < today:
        error('release', 'A new release cannot use a past release date.')
    if original_date and original_date > today:
        error('release', 'The original release date cannot be in the future.')
    if original_date and release_date and original_date > release_date:
        error('release', 'The original release date cannot be after the planned release date.')
    if metadata.get('preorder_enabled'):
        preorder = parse_date(str(metadata.get('preorder_date') or ''))
        if not preorder:
            error('release', 'Choose a valid preorder date.')
        elif preorder < today:
            error('release', 'The preorder date cannot be in the past.')
        elif release_date and preorder >= release_date:
            error('release', 'The preorder date must be before the release date.')

    upc = ''.join(ch for ch in str(metadata.get('upc') or '') if ch.isdigit())
    if metadata.get('upc') and not _valid_gtin(upc):
        error('release', 'UPC/EAN must be a valid 12, 13, or 14 digit GTIN with a correct check digit.')
    if not metadata.get('primary_genre_id'):
        error('release', 'Choose a primary release genre.')
    if metadata.get('secondary_genre_id') and metadata.get('secondary_genre_id') == metadata.get('primary_genre_id'):
        error('release', 'Primary and secondary release genres must be different.')
    release_genre_ids = [value for value in (metadata.get('primary_genre_id'), metadata.get('secondary_genre_id')) if value]
    existing_release_genres = set(Genre.objects.filter(id__in=release_genre_ids).values_list('id', flat=True))
    missing_release_genres = [value for value in release_genre_ids if value not in existing_release_genres]
    if missing_release_genres:
        error('release', f'Release genre IDs do not exist: {missing_release_genres}.')
    shared = merged_shared(release.shared_metadata)
    taxonomy_checks = (
        ('genre', Genre, shared.get('genre_ids') or []),
        ('subgenre', SubGenre, shared.get('sub_genre_ids') or []),
        ('mood', Mood, shared.get('mood_ids') or []),
        ('tag', Tag, shared.get('tag_ids') or []),
    )
    for label, model, values in taxonomy_checks:
        existing_ids = set(model.objects.filter(id__in=values).values_list('id', flat=True))
        missing_ids = [value for value in values if value not in existing_ids]
        if missing_ids:
            error('release', f'Shared {label} IDs do not exist: {missing_ids}.')
    if upc and ArtistRelease.objects.filter(
        release_metadata__upc=upc,
        status__in=[
            ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_APPROVED,
            ArtistRelease.STATUS_SCHEDULED, ArtistRelease.STATUS_LIVE,
        ],
    ).exclude(pk=release.pk).exists():
        error('release', 'This UPC/EAN is already assigned to another active release.')
    if not metadata.get('territories'):
        error('release', 'Select at least one territory.')
    if not metadata.get('platforms'):
        error('release', 'Select at least one platform.')
    if not metadata.get('p_copyright'):
        warning('rights', 'Add the sound recording (P-line) copyright.')
    if not metadata.get('c_copyright'):
        warning('rights', 'Add the artwork/composition (C-line) copyright.')

    release_artist_ids = _clean_int_list(metadata.get('release_artist_ids'))
    if len(release_artist_ids) > 50:
        error('release', 'A release cannot contain more than 50 release-level artists.')
    if release.artist_id not in release_artist_ids:
        error('release', 'The primary artist must remain a release artist.')
    existing_artist_ids = set(Artist.objects.filter(id__in=release_artist_ids).values_list('id', flat=True))
    missing_artist_ids = [value for value in release_artist_ids if value not in existing_artist_ids]
    if missing_artist_ids:
        error('release', f'Release artist IDs do not exist: {missing_artist_ids}.')
    unverified_artist_ids = list(Artist.objects.filter(id__in=release_artist_ids, verified=False).exclude(id=release.artist_id).values_list('id', flat=True))
    if unverified_artist_ids:
        error('release', f'Additional release artists must be verified: {unverified_artist_ids}.')

    complete_tracks = 0
    audio_passed = True
    rights_warnings = 0
    seen_isrc: set[str] = set()
    seen_positions: set[tuple[int, int]] = set()
    last_disc = 1
    disc_track_counts: dict[int, int] = {}
    seen_title_versions: set[tuple[str, str]] = set()
    for link in tracks:
        song = link.song
        extras = normalize_track_extras(link.extras, link.position)
        completion, missing = _track_completion(song, extras)
        if completion >= 75:
            complete_tracks += 1
        if not song.audio_file:
            audio_passed = False
            error('audio', 'Audio file is missing.', song.id)
        if not song.title.strip():
            error('tracks', 'Track title is required.', song.id)
        if not song.language:
            error('tracks', 'Track language is required.', song.id)
        if not song.genres.exists():
            error('tracks', 'Choose at least one genre for this track.', song.id)
        unverified_featured = list(song.featured_artists.filter(verified=False).exclude(id=release.artist_id).values_list('id', flat=True))
        if unverified_featured:
            error('tracks', f'Featured artists must be verified: {unverified_featured}.', song.id)
        if not song.duration_seconds or song.duration_seconds <= 0:
            warning('audio', 'Audio duration could not be verified.', song.id)
        preview_start = max(0, int(extras.get('preview_start') or 0))
        if song.duration_seconds and preview_start >= song.duration_seconds:
            error('audio', 'Preview start must be before the end of the track.', song.id)
        if song.original_format and str(song.original_format).lower() not in {'mp3', 'wav'}:
            error('audio', 'Only MP3 and WAV source files are supported.', song.id)
        version_key = str(extras.get('version') or '').strip().casefold()
        title_key = str(song.title or '').strip().casefold()
        title_version = (title_key, version_key)
        if title_key and title_version in seen_title_versions:
            warning('tracks', 'Another track has the same title and version.', song.id)
        seen_title_versions.add(title_version)
        disc = _positive_int(extras.get('disc_number'), 1, 99)
        track_number = _positive_int(extras.get('track_number'), link.position, 999)
        slot = (disc, track_number)
        if not seen_positions and disc != 1:
            error('tracklist', 'The first disc number must be 1.', song.id)
        if disc < last_disc:
            error('tracklist', 'Tracks from an earlier disc cannot appear after a later disc.', song.id)
        elif disc > last_disc + 1:
            error('tracklist', 'Disc numbers must be continuous without gaps.', song.id)
        last_disc = max(last_disc, disc)
        expected_track = disc_track_counts.get(disc, 0) + 1
        if track_number != expected_track:
            error('tracklist', f'Disc {disc} track numbers must be sequential; expected {expected_track}.', song.id)
        disc_track_counts[disc] = expected_track
        if slot in seen_positions:
            error('tracklist', f'Disc {disc}, track {track_number} is used more than once.', song.id)
        seen_positions.add(slot)
        isrc = str(extras.get('isrc') or '').replace('-', '').replace(' ', '').upper()
        if release.previously_released and not isrc:
            error('rights', 'Previously released recordings require an ISRC.', song.id)
        if isrc:
            if not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{3}[0-9]{7}', isrc):
                error('rights', 'ISRC must follow the 12-character country/registrant/year/designation format.', song.id)
            elif isrc in seen_isrc:
                error('rights', 'The same ISRC cannot be used twice in one release.', song.id)
            elif ArtistReleaseTrack.objects.filter(
                extras__isrc=isrc,
                release__status__in=[
                    ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_APPROVED,
                    ArtistRelease.STATUS_SCHEDULED, ArtistRelease.STATUS_LIVE,
                ],
            ).exclude(release=release).exists():
                warning('rights', 'This ISRC is already used by another active release; confirm that it is the same recording.', song.id)
            seen_isrc.add(isrc)
        splits = extras.get('songwriter_splits') or []
        if splits:
            try:
                valid_splits = [item for item in splits if isinstance(item, dict)]
                shares = [Decimal(str(item.get('share') or 0)) for item in valid_splits]
                total = sum(shares, Decimal('0'))
            except (InvalidOperation, TypeError, ValueError):
                valid_splits, shares, total = [], [], Decimal('-1')
            if len(valid_splits) != len(splits) or any(not str(item.get('name') or '').strip() for item in valid_splits):
                error('rights', 'Every songwriter split requires a contributor name.', song.id)
            if any(share < 0 or share > 100 for share in shares):
                error('rights', 'Each songwriter share must be between 0% and 100%.', song.id)
            if abs(total - Decimal('100')) > Decimal('0.01'):
                error('rights', 'Songwriter splits must total 100%.', song.id)
        for item in missing:
            if item in ('composer', 'lyricist', 'master owner', 'publishing owner'):
                warning('rights', f'Consider completing {item}.', song.id)
                rights_warnings += 1

    return {
        'valid': not errors,
        'errors': errors,
        'warnings': warnings,
        'summary': {
            'release_information': not any(item['section'] == 'release' for item in errors),
            'artwork': bool(cover_url),
            'track_count': count,
            'audio_passed': audio_passed,
            'complete_tracks': complete_tracks,
            'rights_warnings': rights_warnings,
        },
    }


def serialize_release(release: ArtistRelease, request=None, include_history=False) -> dict:
    metadata = merged_release_metadata(release.release_metadata, release.artist_id)
    release_artists = list(Artist.objects.filter(id__in=metadata['release_artist_ids']))
    artist_map = {item.id: item for item in release_artists}
    ordered_artists = [artist_map[value] for value in metadata['release_artist_ids'] if value in artist_map]
    links = list(release.release_tracks.all())
    validation = release.validation_snapshot or validation_payload(release)
    is_staff = bool(request and getattr(getattr(request, 'user', None), 'is_staff', False))
    result = {
        'id': str(release.id),
        'album_id': release.album_id,
        'title': release.title,
        'title_en': release.title_en,
        'release_type': release.release_type,
        'previously_released': release.previously_released,
        'primary_artist_id': release.artist_id,
        'primary_artist': artist_payload(release.artist),
        'release_artists': [artist_payload(item) for item in ordered_artists],
        'status': release.status,
        'current_step': release.current_step,
        'track_ids': [link.song_id for link in links],
        'tracks': [serialize_track(link, request) for link in links],
        'shared_metadata': merged_shared(release.shared_metadata),
        'release_metadata': metadata,
        'track_extras': {str(link.song_id): normalize_track_extras(link.extras, link.position) for link in links},
        'validation': validation,
        'review_note': release.review_note,
        'admin_note': release.admin_note if is_staff else '',
        'lock_version': release.lock_version,
        'revision_number': release.revision_number,
        'source_release_id': str(release.source_release_id) if release.source_release_id else None,
        'created_at': release.created_at,
        'updated_at': release.updated_at,
        'submitted_at': release.submitted_at,
        'reviewed_at': release.reviewed_at,
        'scheduled_at': release.scheduled_at,
        'published_at': release.published_at,
        'taken_down_at': release.taken_down_at,
    }
    if include_history:
        result['status_history'] = [
            {
                'id': item.id,
                'from_status': item.from_status,
                'to_status': item.to_status,
                'note': item.note,
                'actor_id': item.actor_id if is_staff else None,
                'created_at': item.created_at,
            }
            for item in release.status_history.all()
        ]
    return result


def snapshot_song(song: Song) -> dict:
    result = {field: getattr(song, field) for field in TRACK_METADATA_FIELDS}
    result.update({
        'audio_file': song.audio_file,
        'converted_audio_url': song.converted_audio_url,
        'preview_audio_url': song.preview_audio_url,
        'cover_image': song.cover_image,
        'original_format': song.original_format,
        'duration_seconds': song.duration_seconds,
        'featured_artist_ids': list(song.featured_artists.values_list('id', flat=True)),
        'genre_ids': list(song.genres.values_list('id', flat=True)),
        'sub_genre_ids': list(song.sub_genres.values_list('id', flat=True)),
        'mood_ids': list(song.moods.values_list('id', flat=True)),
        'tag_ids': list(song.tags.values_list('id', flat=True)),
    })
    for key, value in list(result.items()):
        if hasattr(value, 'isoformat'):
            result[key] = value.isoformat()
    return result


def duplicate_song(song: Song, uploader=None) -> Song:
    values = {
        'title': song.title,
        'title_en': song.title_en,
        'artist': song.artist,
        'album': None,
        'is_single': False,
        'album_disc_number': song.album_disc_number,
        'album_track_number': song.album_track_number,
        'audio_file': song.audio_file,
        'converted_audio_url': song.converted_audio_url,
        'preview_audio_url': song.preview_audio_url,
        'preview_generated_at': song.preview_generated_at,
        'preview_error': song.preview_error,
        'preview_attempts': song.preview_attempts,
        'preview_last_attempt_at': song.preview_last_attempt_at,
        'cover_image': song.cover_image,
        'original_format': song.original_format,
        'duration_seconds': song.duration_seconds,
        'status': Song.STATUS_DRAFT,
        'release_date': song.release_date,
        'language': song.language,
        'description': song.description,
        'description_en': song.description_en,
        'lyrics': song.lyrics,
        'lyrics_en': song.lyrics_en,
        'tempo': song.tempo,
        'energy': song.energy,
        'danceability': song.danceability,
        'valence': song.valence,
        'acousticness': song.acousticness,
        'instrumentalness': song.instrumentalness,
        'live_performed': song.live_performed,
        'speechiness': song.speechiness,
        'label': song.label,
        'label_en': song.label_en,
        'producers': deepcopy(song.producers or []),
        'producers_en': deepcopy(song.producers_en or []),
        'composers': deepcopy(song.composers or []),
        'composers_en': deepcopy(song.composers_en or []),
        'lyricists': deepcopy(song.lyricists or []),
        'lyricists_en': deepcopy(song.lyricists_en or []),
        'credits': song.credits,
        'credits_en': song.credits_en,
        'uploader': uploader or song.uploader,
    }
    copy = Song.objects.create(**values)
    copy.featured_artists.set(song.featured_artists.all())
    copy.genres.set(song.genres.all())
    copy.sub_genres.set(song.sub_genres.all())
    copy.moods.set(song.moods.all())
    copy.tags.set(song.tags.all())
    return copy


def ensure_editable_song(release: ArtistRelease, song: Song, uploader=None) -> tuple[Song, Song | None]:
    linked_elsewhere = ArtistReleaseTrack.objects.filter(song=song).exclude(release=release).exists()
    immutable = song.status != Song.STATUS_DRAFT or song.album_id is not None or linked_elsewhere
    if immutable:
        return duplicate_song(song, uploader=uploader), song
    return song, None


def change_status(release: ArtistRelease, to_status: str, actor=None, note='') -> None:
    previous = release.status
    release.status = to_status
    release.review_note = note or release.review_note
    release.reviewed_at = timezone.now() if to_status != ArtistRelease.STATUS_DRAFT else None
    release.lock_version += 1
    release.save(update_fields=['status', 'review_note', 'reviewed_at', 'lock_version', 'updated_at'])
    ArtistReleaseStatusHistory.objects.create(
        release=release, from_status=previous, to_status=to_status, note=note or '', actor=actor
    )


def create_revision(source: ArtistRelease, uploader=None, mode='duplicate') -> ArtistRelease:
    with transaction.atomic():
        metadata = merged_release_metadata(source.release_metadata, source.artist_id)
        metadata['upc'] = '' if mode == 'duplicate' else metadata.get('upc', '')
        copy = ArtistRelease.objects.create(
            artist=source.artist,
            title=f"{source.title} ({'Revision' if mode == 'revision' else 'Copy'})",
            title_en=source.title_en,
            release_type=source.release_type,
            status=ArtistRelease.STATUS_DRAFT,
            previously_released=source.previously_released,
            current_step=1,
            shared_metadata=deepcopy(source.shared_metadata or {}),
            release_metadata=metadata,
            source_release=source,
            revision_number=(source.revision_number or 1) + 1,
        )
        for link in source.release_tracks.all():
            song_copy = duplicate_song(link.song, uploader=uploader)
            ArtistReleaseTrack.objects.create(
                release=copy,
                song=song_copy,
                source_song=link.song,
                position=link.position,
                extras=normalize_track_extras(link.extras, link.position),
            )
        return release_queryset().get(pk=copy.pk)


def apply_track_metadata(song: Song, metadata: dict) -> None:
    """Validate bulk metadata through the existing song contract before saving."""
    payload = {field: metadata[field] for field in TRACK_METADATA_FIELDS if field in metadata}
    for input_key in TRACK_RELATION_FIELDS:
        if input_key not in metadata:
            continue
        output_key = input_key if input_key == 'featured_artist_ids' else f'{input_key}_write'
        payload[output_key] = _clean_int_list(metadata.get(input_key))
    serializer = SongSerializer(song, data=payload, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()


def scheduled_datetime(release: ArtistRelease):
    date_value = parse_date(str(merged_release_metadata(release.release_metadata).get('release_date') or ''))
    if not date_value:
        return None
    return timezone.make_aware(datetime.combine(date_value, time.min), timezone.get_current_timezone())



def prepare_release(release: ArtistRelease, schedule=False) -> ArtistRelease:
    """Freeze validated tracks without exposing legacy catalog records.

    Album has no public/private status in the legacy schema, so an Album row is
    intentionally created only by ``materialize_release(..., publish=True)``.
    """
    with transaction.atomic():
        release = release_queryset().select_for_update().get(pk=release.pk)
        for link in release.release_tracks.all():
            Song.objects.filter(pk=link.song_id).update(status=Song.STATUS_APPROVED)
        release.scheduled_at = scheduled_datetime(release) if schedule else None
        release.save(update_fields=['scheduled_at', 'updated_at'])
        return release

def materialize_release(release: ArtistRelease, publish=True) -> ArtistRelease:
    if not publish:
        return prepare_release(release, schedule=False)
    with transaction.atomic():
        release = release_queryset().select_for_update().get(pk=release.pk)
        metadata = merged_release_metadata(release.release_metadata, release.artist_id)
        release_date = parse_date(str(metadata.get('release_date') or ''))
        cover_url = str(metadata.get('cover_url') or '')
        links = list(release.release_tracks.all())
        song_status = Song.STATUS_PUBLISHED if publish else Song.STATUS_APPROVED

        release_song_ids = [link.song_id for link in links]
        album = release.album
        # A legacy/admin edit could attach unrelated songs to the materialized
        # album. Never mutate or delete that shared album; create a clean catalog
        # row for this release and leave unrelated content untouched.
        album_has_unrelated_songs = bool(album) and album.songs.exclude(pk__in=release_song_ids).exists()
        if release.release_type == ArtistRelease.TYPE_SINGLE:
            if album and not album_has_unrelated_songs:
                album.delete()
            album = None
            for link in links:
                song = link.song
                extras = normalize_track_extras(link.extras, link.position)
                song.album = None
                song.is_single = True
                song.album_disc_number = _positive_int(extras.get('disc_number'), 1, 99)
                song.album_track_number = _positive_int(extras.get('track_number'), link.position, 999)
                song.status = song_status
                song.release_date = release_date
                if cover_url:
                    song.cover_image = cover_url
                song.save(update_fields=['album', 'is_single', 'album_disc_number', 'album_track_number', 'status', 'release_date', 'cover_image', 'updated_at'])
        else:
            if album_has_unrelated_songs:
                album = None
            if not album:
                album = Album.objects.create(
                    title=release.title,
                    title_en=release.title_en,
                    artist=release.artist,
                    cover_image=cover_url,
                    release_date=release_date,
                    description=metadata.get('description') or '',
                    description_en=metadata.get('description_en') or '',
                )
            else:
                album.title = release.title
                album.title_en = release.title_en
                album.cover_image = cover_url
                album.release_date = release_date
                album.description = metadata.get('description') or ''
                album.description_en = metadata.get('description_en') or ''
                album.save()
            genre_ids = [value for value in [metadata.get('primary_genre_id'), metadata.get('secondary_genre_id')] if value]
            album.genres.set(_clean_int_list(genre_ids))
            album.sub_genres.set(_clean_int_list((release.shared_metadata or {}).get('sub_genre_ids')))
            album.moods.set(_clean_int_list((release.shared_metadata or {}).get('mood_ids')))
            for link in links:
                song = link.song
                extras = normalize_track_extras(link.extras, link.position)
                song.album = album
                song.is_single = False
                song.album_disc_number = _positive_int(extras.get('disc_number'), 1, 99)
                song.album_track_number = _positive_int(extras.get('track_number'), link.position, 999)
                song.status = song_status
                song.release_date = release_date
                if cover_url:
                    song.cover_image = cover_url
                song.save(update_fields=['album', 'is_single', 'album_disc_number', 'album_track_number', 'status', 'release_date', 'cover_image', 'updated_at'])

        release.album = album
        release.scheduled_at = scheduled_datetime(release) if not publish else release.scheduled_at
        release.published_at = timezone.now() if publish else release.published_at
        release.taken_down_at = None
        release.save(update_fields=['album', 'scheduled_at', 'published_at', 'taken_down_at', 'updated_at'])
        return release


def take_down_release(release: ArtistRelease) -> None:
    with transaction.atomic():
        release = ArtistRelease.objects.select_for_update().select_related('album').get(pk=release.pk)
        release_song_ids = list(release.release_tracks.values_list('song_id', flat=True))
        Song.objects.filter(pk__in=release_song_ids).update(status=Song.STATUS_APPROVED)
        # Legacy Album has no visibility state and public endpoints query every
        # row. Delete the materialized album only when it belongs exclusively to
        # this release. The unrelated-song guard prevents accidental catalog loss
        # if old/admin data attached another recording to the same album.
        album = release.album
        can_delete_album = bool(album) and not album.songs.exclude(pk__in=release_song_ids).exists()
        release.album = None
        release.taken_down_at = timezone.now()
        release.save(update_fields=['album', 'taken_down_at', 'updated_at'])
        if can_delete_album:
            album.delete()
        elif album is not None:
            Song.objects.filter(pk__in=release_song_ids, album=album).update(album=None, is_single=False)


def publish_due_releases(limit=50) -> int:
    now = timezone.now()
    queryset = ArtistRelease.objects.filter(
        status=ArtistRelease.STATUS_SCHEDULED,
        scheduled_at__isnull=False,
        scheduled_at__lte=now,
    ).order_by('scheduled_at')[:limit]
    published = 0
    for release in queryset:
        try:
            with transaction.atomic():
                locked = ArtistRelease.objects.select_for_update().get(pk=release.pk)
                if locked.status != ArtistRelease.STATUS_SCHEDULED or not locked.scheduled_at or locked.scheduled_at > now:
                    continue
                materialize_release(locked, publish=True)
                change_status(locked, ArtistRelease.STATUS_LIVE, note='Automatically published at the scheduled time.')
                published += 1
        except ArtistRelease.DoesNotExist:
            continue
    return published
