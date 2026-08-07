from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta
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
from .utils import generate_signed_r2_url


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
    'label': '',
    'label_en': '',
    'p_copyright': '',
    'c_copyright': '',
    'territories': ['WORLDWIDE'],
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



def merged_shared(value: dict | None) -> dict:
    result = deepcopy(DEFAULT_SHARED_METADATA)
    if isinstance(value, dict):
        for key in DEFAULT_SHARED_METADATA:
            if key in value:
                result[key] = value[key]
    for key in ('genre_ids', 'sub_genre_ids', 'mood_ids', 'tag_ids'):
        result[key] = _clean_int_list(result.get(key))
    for key in ('producers', 'producers_en', 'composers', 'composers_en', 'lyricists', 'lyricists_en'):
        result[key] = _clean_string_list(result.get(key))
    result['language'] = str(result.get('language') or 'fa').strip() or 'fa'
    result['label'] = str(result.get('label') or '').strip()
    result['label_en'] = str(result.get('label_en') or '').strip()
    return result

def merged_release_metadata(value: dict | None, artist_id: int | None = None) -> dict:
    result = deepcopy(DEFAULT_RELEASE_METADATA)
    if isinstance(value, dict):
        for key in DEFAULT_RELEASE_METADATA:
            if key in value:
                result[key] = value[key]
    result['territories'] = _clean_string_list(result.get('territories')) or ['WORLDWIDE']
    for key in (
        'release_date', 'original_release_date', 'label', 'label_en', 'p_copyright',
        'c_copyright', 'cover_url', 'description', 'description_en',
    ):
        result[key] = str(result.get(key) or '').strip()
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




def normalize_track_extras(value: dict | None, default_position: int = 1) -> dict:
    source = value if isinstance(value, dict) else {}
    result = {
        'isrc': re.sub(r'[-\s]', '', str(source.get('isrc') or '')).upper(),
        'version': str(source.get('version') or '').strip(),
        'explicit': _as_bool(source.get('explicit', False)),
        'publishing_owner': str(source.get('publishing_owner') or '').strip(),
    }
    try:
        result['preview_start'] = max(0, int(source.get('preview_start') or 0))
    except (TypeError, ValueError):
        result['preview_start'] = 0
    if source.get('_cover_source') in {'release', 'track'}:
        result['_cover_source'] = source['_cover_source']
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
    if not str(extras.get('publishing_owner') or '').strip():
        missing.append('publishing owner')
    return missing


def _track_completion(song: Song, extras: dict) -> tuple[int, list[str]]:
    missing = _track_missing(song, extras)
    total = 7
    return max(0, round((total - len(missing)) / total * 100)), missing


def serialize_track(link: ArtistReleaseTrack, request=None, fallback_cover="") -> dict:
    data = dict(SongSerializer(link.song, context={'request': request, 'artist_panel': True}).data)
    cover_source = str((link.extras or {}).get('_cover_source') or '')
    own_cover = bool(data.get('cover_image')) and cover_source != 'release' and not (not cover_source and fallback_cover and data.get('cover_image') == fallback_cover)
    data['own_cover_image'] = own_cover
    if not own_cover and fallback_cover:
        data['cover_image'] = fallback_cover
    completion, missing = _track_completion(link.song, link.extras or {})
    data.update({
        'has_audio': bool(link.song.audio_file),
        'metadata_completion': completion,
        'missing_metadata': missing,
        'release_extras': normalize_track_extras(link.extras, link.position),
    })
    return data

def validation_payload(release: ArtistRelease) -> dict:
    errors: list[dict] = []
    warnings: list[dict] = []
    metadata = merged_release_metadata(release.release_metadata, release.artist_id)
    shared = merged_shared(release.shared_metadata)
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

    if not str(release.title or '').strip() or release.title.strip().lower() == 'انتشار بدون عنوان':
        error('release', 'عنوان انتشار را وارد کنید.')
    if release.release_type not in dict(ArtistRelease.TYPE_CHOICES):
        error('release', 'یک نوع انتشار معتبر انتخاب کنید.')

    count = len(tracks)
    if release.release_type == ArtistRelease.TYPE_SINGLE and count != 1:
        error('tracklist', 'انتشار تک‌آهنگ باید دقیقاً یک ترک داشته باشد.')
    elif release.release_type == ArtistRelease.TYPE_EP and not (2 <= count <= 7):
        error('tracklist', 'مینی‌آلبوم باید بین ۲ تا ۷ ترک داشته باشد.')
    elif release.release_type in (ArtistRelease.TYPE_ALBUM, ArtistRelease.TYPE_COMPILATION) and count < 2:
        error('tracklist', 'این نوع انتشار باید حداقل دو ترک داشته باشد.')
    if count > 100:
        error('tracklist', 'هر انتشار می‌تواند حداکثر ۱۰۰ ترک داشته باشد.')

    cover_url = str(metadata.get('cover_url') or '').strip()
    if not cover_url:
        error('artwork', 'یک کاور مربعی برای انتشار بارگذاری کنید.')
    release_date = parse_date(str(metadata.get('release_date') or ''))
    if not release_date:
        error('release', 'یک تاریخ انتشار معتبر انتخاب کنید.')
    original_date = parse_date(str(metadata.get('original_release_date') or '')) if metadata.get('original_release_date') else None
    if release.previously_released and not original_date:
        error('release', 'برای محتوای قبلاً منتشرشده، تاریخ انتشار اصلی را وارد کنید.')
    today = timezone.localdate()
    if release_date and not release.previously_released and release_date < today:
        error('release', 'برای انتشار جدید نمی‌توان تاریخ گذشته انتخاب کرد.')
    # Allow a one-day calendar skew between the artist device and a UTC server.
    if original_date and original_date > today + timedelta(days=1):
        error('release', 'تاریخ انتشار اصلی نمی‌تواند در آینده باشد.')
    if original_date and release_date and original_date > release_date:
        error('release', 'تاریخ انتشار اصلی نمی‌تواند بعد از تاریخ برنامه‌ریزی‌شده باشد.')

    if not shared.get('genre_ids'):
        error('release', 'در بخش دسته‌بندی مشترک حداقل یک ژانر انتخاب کنید.')
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
            error('release', f'شناسه‌های انتخاب‌شده برای {label} معتبر نیستند: {missing_ids}.')

    if not metadata.get('territories'):
        error('release', 'حداقل یک قلمرو انتشار انتخاب کنید.')
    if not metadata.get('p_copyright'):
        warning('rights', 'اطلاعات حق مالکیت ضبط (P-line) را وارد کنید.')
    if not metadata.get('c_copyright'):
        warning('rights', 'اطلاعات حق مالکیت اثر و کاور (C-line) را وارد کنید.')

    complete_tracks = 0
    audio_passed = True
    rights_warnings = 0
    seen_isrc: set[str] = set()
    seen_title_versions: set[tuple[str, str]] = set()
    for link in tracks:
        song = link.song
        if song.status == Song.STATUS_DELETED:
            error('tracks', 'فایل‌های حذف‌شده قابل ارسال یا انتشار نیستند.', song.id)
        extras = normalize_track_extras(link.extras, link.position)
        completion, missing = _track_completion(song, extras)
        if completion >= 75:
            complete_tracks += 1
        if not song.audio_file:
            audio_passed = False
            error('audio', 'فایل صوتی موجود نیست.', song.id)
        if not str(song.title or '').strip():
            error('tracks', 'وارد کردن عنوان ترک الزامی است.', song.id)
        if not song.language:
            error('tracks', 'انتخاب زبان ترک الزامی است.', song.id)
        if not song.genres.exists():
            error('tracks', 'برای این انتشار حداقل یک ژانر مشترک انتخاب کنید.', song.id)
        if not song.duration_seconds or song.duration_seconds <= 0:
            warning('audio', 'مدت فایل صوتی قابل تأیید نبود.', song.id)
        preview_start = max(0, int(extras.get('preview_start') or 0))
        if song.duration_seconds and preview_start >= song.duration_seconds:
            error('audio', 'زمان شروع پیش‌نمایش باید پیش از پایان ترک باشد.', song.id)
        if song.original_format and str(song.original_format).lower() not in {'mp3', 'wav'}:
            error('audio', 'فقط فایل‌های منبع MP3 و WAV پشتیبانی می‌شوند.', song.id)
        version_key = str(extras.get('version') or '').strip().casefold()
        title_key = str(song.title or '').strip().casefold()
        title_version = (title_key, version_key)
        if title_key and title_version in seen_title_versions:
            warning('tracks', 'ترک دیگری با همین عنوان و نسخه وجود دارد.', song.id)
        seen_title_versions.add(title_version)
        isrc = str(extras.get('isrc') or '').replace('-', '').replace(' ', '').upper()
        if release.previously_released and not isrc:
            error('rights', 'برای فایل‌های قبلاً منتشرشده وارد کردن ISRC الزامی است.', song.id)
        if isrc:
            if not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{3}[0-9]{7}', isrc):
                error('rights', 'کد ISRC باید مطابق ساختار ۱۲ کاراکتری کشور، ثبت‌کننده، سال و شناسه اثر باشد.', song.id)
            elif isrc in seen_isrc:
                error('rights', 'یک کد ISRC را نمی‌توان دو بار در یک انتشار استفاده کرد.', song.id)
            elif ArtistReleaseTrack.objects.filter(
                extras__isrc=isrc,
                release__status__in=[
                    ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_APPROVED,
                    ArtistRelease.STATUS_SCHEDULED, ArtistRelease.STATUS_LIVE,
                ],
            ).exclude(release=release).exists():
                warning('rights', 'این کد ISRC در انتشار فعال دیگری استفاده شده است؛ یکسان بودن فایل ضبط‌شده را بررسی کنید.', song.id)
            seen_isrc.add(isrc)
        rights_labels = {
            'composer': 'آهنگساز',
            'lyricist': 'ترانه‌سرا',
            'publishing owner': 'مالک حقوق نشر',
        }
        for item in missing:
            if item in rights_labels:
                warning('rights', f'تکمیل اطلاعات {rights_labels[item]} پیشنهاد می‌شود.', song.id)
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
    raw_cover_url = metadata.get('cover_url')
    if raw_cover_url:
        try:
            metadata['cover_url'] = generate_signed_r2_url(raw_cover_url) or raw_cover_url
        except Exception:
            metadata['cover_url'] = raw_cover_url
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
        'status': release.status,
        'current_step': max(1, min(5, release.current_step or 1)),
        'track_ids': [link.song_id for link in links],
        'tracks': [serialize_track(link, request, metadata.get('cover_url')) for link in links],
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


def sync_release_artwork(release: ArtistRelease, previous_cover: str = '') -> None:
    """Persist release artwork onto linked song rows without destroying track covers.

    A single has one canonical cover, so its song row always receives the release
    cover immediately. Collection tracks inherit the release cover only while
    they do not own a dedicated cover. Stable, unsigned R2 values are stored in
    the database; response middleware signs them for the Artist Panel.
    """
    cover_url = str((release.release_metadata or {}).get('cover_url') or '').strip()
    if not cover_url:
        return
    links = ArtistReleaseTrack.objects.select_related('song').filter(release=release)
    for link in links:
        song = link.song
        extras = dict(link.extras or {})
        source = str(extras.get('_cover_source') or '')
        if release.release_type == ArtistRelease.TYPE_SINGLE:
            should_inherit = True
        else:
            should_inherit = (
                source == 'release'
                or not song.cover_image
                or (bool(previous_cover) and song.cover_image == previous_cover)
            )
        if not should_inherit:
            continue
        changed_song = song.cover_image != cover_url
        changed_extras = source != 'release'
        if changed_song:
            Song.objects.filter(pk=song.pk).update(cover_image=cover_url, updated_at=timezone.now())
        if changed_extras:
            extras['_cover_source'] = 'release'
            ArtistReleaseTrack.objects.filter(pk=link.pk).update(extras=extras, updated_at=timezone.now())


def sync_release_tracks(release: ArtistRelease) -> None:
    """Apply release-level metadata to linked tracks and keep artwork inheritance aligned."""
    shared = merged_shared(release.shared_metadata)
    metadata = merged_release_metadata(release.release_metadata, release.artist_id)
    common = {
        'release_date': metadata.get('release_date') or None,
        'language': shared.get('language') or 'fa',
        'label': shared.get('label') or metadata.get('label') or '',
        'label_en': shared.get('label_en') or metadata.get('label_en') or '',
        'genre_ids': shared.get('genre_ids') or [],
        'sub_genre_ids': shared.get('sub_genre_ids') or [],
        'mood_ids': shared.get('mood_ids') or [],
        'tag_ids': shared.get('tag_ids') or [],
        'producers': shared.get('producers') or [],
        'producers_en': shared.get('producers_en') or [],
        'composers': shared.get('composers') or [],
        'composers_en': shared.get('composers_en') or [],
        'lyricists': shared.get('lyricists') or [],
        'lyricists_en': shared.get('lyricists_en') or [],
    }
    links = ArtistReleaseTrack.objects.select_related('song').filter(release=release).order_by('position', 'id')
    for link in links:
        payload = dict(common)
        if release.release_type == ArtistRelease.TYPE_SINGLE:
            payload['title'] = release.title
            payload['title_en'] = release.title_en
        apply_track_metadata(link.song, payload)
    sync_release_artwork(release)


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
        ArtistRelease.objects.select_for_update().only('pk').get(pk=release.pk)
        release = release_queryset().get(pk=release.pk)
        sync_release_tracks(release)
        for link in release.release_tracks.all():
            Song.objects.filter(pk=link.song_id).exclude(status=Song.STATUS_DELETED).update(
                status=Song.STATUS_APPROVED
            )
        release.scheduled_at = scheduled_datetime(release) if schedule else None
        release.save(update_fields=['scheduled_at', 'updated_at'])
        return release

def materialize_release(release: ArtistRelease, publish=True) -> ArtistRelease:
    if not publish:
        return prepare_release(release, schedule=False)
    with transaction.atomic():
        ArtistRelease.objects.select_for_update().only('pk').get(pk=release.pk)
        release = release_queryset().get(pk=release.pk)
        sync_release_tracks(release)
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
                song.album = None
                song.is_single = True
                song.album_disc_number = 1
                song.album_track_number = link.position
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
            shared = merged_shared(release.shared_metadata)
            album.genres.set(shared.get('genre_ids') or [])
            album.sub_genres.set(shared.get('sub_genre_ids') or [])
            album.moods.set(shared.get('mood_ids') or [])
            for link in links:
                song = link.song
                song.album = album
                song.is_single = False
                song.album_disc_number = 1
                song.album_track_number = link.position
                song.status = song_status
                song.release_date = release_date
                extras = dict(link.extras or {})
                cover_source = str(extras.get('_cover_source') or '')
                if cover_url and (not song.cover_image or cover_source == 'release'):
                    song.cover_image = cover_url
                    extras['_cover_source'] = 'release'
                    if extras != (link.extras or {}):
                        link.extras = extras
                        link.save(update_fields=['extras', 'updated_at'])
                song.save(update_fields=['album', 'is_single', 'album_disc_number', 'album_track_number', 'status', 'release_date', 'cover_image', 'updated_at'])

        release.album = album
        release.scheduled_at = scheduled_datetime(release) if not publish else release.scheduled_at
        release.published_at = timezone.now() if publish else release.published_at
        release.taken_down_at = None
        release.save(update_fields=['album', 'scheduled_at', 'published_at', 'taken_down_at', 'updated_at'])
        return release


def take_down_release(release: ArtistRelease) -> None:
    with transaction.atomic():
        ArtistRelease.objects.select_for_update().only('pk').get(pk=release.pk)
        release = ArtistRelease.objects.select_related('album').get(pk=release.pk)
        release_song_ids = list(release.release_tracks.values_list('song_id', flat=True))
        Song.objects.filter(pk__in=release_song_ids).exclude(status=Song.STATUS_DELETED).update(
            status=Song.STATUS_APPROVED
        )
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


def mark_release_for_review(
    release: ArtistRelease,
    *,
    actor=None,
    note: str = '',
    song_ids: list[int] | tuple[int, ...] | None = None,
    all_tracks: bool = True,
) -> ArtistRelease:
    """Return an edited release to review without breaking shared published songs.

    Release-level edits move every release-owned recording to pending. A targeted
    song edit only moves that recording. Published source recordings reused by
    another live release are preserved; release additions use editable copies.
    """
    links = ArtistReleaseTrack.objects.select_for_update().filter(release=release)
    if song_ids is not None:
        links = links.filter(song_id__in=list(song_ids))
    elif not all_tracks:
        links = links.none()

    for song_id in links.values_list('song_id', flat=True):
        song = Song.objects.select_for_update().get(pk=song_id)
        if song.status == Song.STATUS_DELETED:
            continue
        shared_live = (
            song.status == Song.STATUS_PUBLISHED
            and ArtistReleaseTrack.objects.filter(song_id=song_id, release__status=ArtistRelease.STATUS_LIVE)
            .exclude(release=release)
            .exists()
        )
        if not shared_live:
            Song.objects.filter(pk=song_id).update(status=Song.STATUS_PENDING)

    release.submitted_at = timezone.now()
    release.scheduled_at = None
    release.validation_snapshot = {}
    release.save(update_fields=['submitted_at', 'scheduled_at', 'validation_snapshot', 'updated_at'])
    review_note = note or 'تغییرات هنرمند نیازمند بررسی دوباره است.'
    if release.status != ArtistRelease.STATUS_IN_REVIEW:
        change_status(release, ArtistRelease.STATUS_IN_REVIEW, actor=actor, note=review_note)
    else:
        release.review_note = review_note
        release.lock_version += 1
        release.save(update_fields=['review_note', 'lock_version', 'updated_at'])
    return release


def approve_release(release: ArtistRelease, *, actor=None, note: str = '') -> ArtistRelease:
    """Approve a new release, or republish an edited release that was live before."""
    if release.published_at:
        release = materialize_release(release, publish=True)
        change_status(
            release, ArtistRelease.STATUS_LIVE, actor=actor,
            note=note or 'انتشار ویرایش‌شده تأیید و دوباره منتشر شد.',
        )
    else:
        release = prepare_release(release, schedule=False)
        change_status(
            release, ArtistRelease.STATUS_APPROVED, actor=actor,
            note=note or 'انتشار تأیید شد.',
        )
    return release


def set_release_status_from_admin(
    release: ArtistRelease,
    target_status: str,
    *,
    actor=None,
    note: str = 'وضعیت از پنل مدیریت تغییر کرد.',
) -> ArtistRelease:
    """Apply a manually selected admin status and synchronize every linked song."""
    if target_status not in dict(ArtistRelease.STATUS_CHOICES):
        raise ValueError('وضعیت انتشار معتبر نیست.')

    # Admin moderation is authoritative for the entire release. Always repair
    # titles, metadata, and cover inheritance before changing lifecycle state.
    sync_release_tracks(release)
    release = release_queryset().get(pk=release.pk)

    if target_status in {ArtistRelease.STATUS_APPROVED, ArtistRelease.STATUS_SCHEDULED, ArtistRelease.STATUS_LIVE}:
        validation = validation_payload(release)
        release.validation_snapshot = validation
        release.save(update_fields=['validation_snapshot', 'updated_at'])
        if not validation['valid']:
            messages = '; '.join(item.get('message', 'انتشار معتبر نیست') for item in validation.get('errors', [])[:5])
            raise ValueError(messages or 'اعتبارسنجی انتشار انجام نشد.')

    if target_status == ArtistRelease.STATUS_DRAFT:
        if release.status == ArtistRelease.STATUS_LIVE:
            take_down_release(release)
            release = ArtistRelease.objects.select_for_update().get(pk=release.pk)
        Song.objects.filter(release_track_links__release=release).exclude(status=Song.STATUS_DELETED).update(status=Song.STATUS_DRAFT)
        release.submitted_at = None
        release.scheduled_at = None
        release.validation_snapshot = {}
        release.save(update_fields=['submitted_at', 'scheduled_at', 'validation_snapshot', 'updated_at'])
        change_status(release, target_status, actor=actor, note=note)
    elif target_status == ArtistRelease.STATUS_IN_REVIEW:
        mark_release_for_review(release, actor=actor, note=note, all_tracks=True)
    elif target_status == ArtistRelease.STATUS_CHANGES_REQUESTED:
        Song.objects.filter(release_track_links__release=release).exclude(status=Song.STATUS_DELETED).update(status=Song.STATUS_PENDING)
        change_status(release, target_status, actor=actor, note=note)
    elif target_status == ArtistRelease.STATUS_REJECTED:
        Song.objects.filter(release_track_links__release=release).exclude(status=Song.STATUS_DELETED).update(status=Song.STATUS_REJECTED)
        change_status(release, target_status, actor=actor, note=note)
    elif target_status == ArtistRelease.STATUS_APPROVED:
        release = approve_release(release, actor=actor, note=note)
    elif target_status == ArtistRelease.STATUS_SCHEDULED:
        scheduled_at = scheduled_datetime(release)
        if not scheduled_at or scheduled_at <= timezone.now():
            raise ValueError('انتشار زمان‌بندی‌شده باید تاریخ آینده داشته باشد.')
        release = prepare_release(release, schedule=True)
        change_status(release, target_status, actor=actor, note=note)
    elif target_status == ArtistRelease.STATUS_LIVE:
        release = materialize_release(release, publish=True)
        change_status(release, target_status, actor=actor, note=note)
    elif target_status == ArtistRelease.STATUS_TAKEN_DOWN:
        if release.status == ArtistRelease.STATUS_LIVE:
            take_down_release(release)
            release = ArtistRelease.objects.select_for_update().get(pk=release.pk)
        else:
            Song.objects.filter(release_track_links__release=release).exclude(status=Song.STATUS_DELETED).update(status=Song.STATUS_APPROVED)
        change_status(release, target_status, actor=actor, note=note)
    return release_queryset().get(pk=release.pk)


def synchronize_release_state(release: ArtistRelease) -> ArtistRelease:
    """Repair linked-song state from the release's current authoritative status.

    This is intentionally idempotent and is used by Django admin after manual
    database-era inconsistencies. It preserves deleted songs and accounting rows.
    """
    with transaction.atomic():
        ArtistRelease.objects.select_for_update().only('pk').get(pk=release.pk)
        release = release_queryset().get(pk=release.pk)
        sync_release_tracks(release)
        songs = Song.objects.filter(release_track_links__release=release).exclude(status=Song.STATUS_DELETED)
        if release.status == ArtistRelease.STATUS_LIVE:
            release = materialize_release(release, publish=True)
        elif release.status in {ArtistRelease.STATUS_APPROVED, ArtistRelease.STATUS_SCHEDULED}:
            songs.update(status=Song.STATUS_APPROVED)
        elif release.status in {ArtistRelease.STATUS_IN_REVIEW, ArtistRelease.STATUS_CHANGES_REQUESTED}:
            songs.update(status=Song.STATUS_PENDING)
        elif release.status == ArtistRelease.STATUS_REJECTED:
            songs.update(status=Song.STATUS_REJECTED)
        elif release.status == ArtistRelease.STATUS_DRAFT:
            songs.update(status=Song.STATUS_DRAFT)
        elif release.status == ArtistRelease.STATUS_TAKEN_DOWN:
            if release.album_id:
                take_down_release(release)
                release = ArtistRelease.objects.get(pk=release.pk)
            songs.update(status=Song.STATUS_APPROVED)
        return release_queryset().get(pk=release.pk)


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
                change_status(locked, ArtistRelease.STATUS_LIVE, note='انتشار در زمان تعیین‌شده به‌صورت خودکار منتشر شد.')
                published += 1
        except ArtistRelease.DoesNotExist:
            continue
    return published
