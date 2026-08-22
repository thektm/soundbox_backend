import os
import re
import uuid

from rest_framework import serializers
from .utils import (
    absolute_api_url, cleanup_r2_urls, generate_signed_r2_url, public_media_url,
    r2_object_key, upload_file_to_r2, user_profile_image_url,
)
from .models import (
    User, UserPlaylist, Artist, ArtistSocialAccount , ArtistAuth, RefreshToken, EventPlaylist, Album, Genre, Mood, Tag, 
    SubGenre, Song, Playlist, StreamAccess, RecommendedPlaylist, SearchSection,
    NotificationSetting, Follow, SongLike, AlbumLike, PlaylistLike, Rules, PlayConfiguration,
    DepositRequest, Report, Notification, AudioAd, UserHistory, DownloadHistory, InitialCheck, UserImageProfile, SupportTicket
)

from .models import BannerAd
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from urllib.parse import urlencode
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.db.models.manager import BaseManager
from django.urls import reverse
from django.utils.text import slugify as django_slugify

from .localization import get_request_language, localized_value, translate_generated_text, generated_playlist_english
from .subscriptions import normalize_expired_premium, premium_expires_at
from .stream_grants import create_stream_grant_url
from .similarity import ranked_similar_song_ids

from .performance import (
    CATALOG_VERSION_KEY, cache_get_or_claim, cache_set, cache_version,
    hydrate_album_metrics, hydrate_artist_full_list, hydrate_artist_metrics, hydrate_playlist_metrics, hydrate_song_metrics, relation_ids, stable_cache_key,
)


def _signed_url(value, expiration=3600):
    if not value:
        return None
    return generate_signed_r2_url(value, expiration=expiration) or value


def _preview_url(song):
    cached = getattr(song, '_signed_preview_url', None)
    if cached:
        return cached
    value = _signed_url(getattr(song, 'preview_audio_url', None), expiration=900)
    if value:
        song._signed_preview_url = value
    return value


def _stream_wrapper(song, request):
    if not request or not request.user.is_authenticated:
        return _preview_url(song)
    user_id = int(request.user.pk)
    grants = getattr(song, '_serializer_stream_grants', None)
    if grants is None:
        grants = {}
        song._serializer_stream_grants = grants
    if user_id not in grants:
        grants[user_id] = create_stream_grant_url(request, song)
    return grants[user_id]


def _metric(obj, attr, fallback):
    value = getattr(obj, attr, None)
    return fallback() if value is None else value


_ARABIC_SCRIPT_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")


def _strict_english_slug(value):
    """Return an ASCII slug only when the source contains no Persian/Arabic text."""
    text = str(value or '').strip()
    if not text or _ARABIC_SCRIPT_RE.search(text):
        return ''
    return django_slugify(text, allow_unicode=False)


def _canonical_url_slug(instance):
    """Language-independent canonical slug from verified English-safe source text.

    ``None`` means the model has no canonical content slug contract. ``''`` means
    the model supports canonical slugs but no real English value is stored, so
    clients must emit the numeric/id-only URL instead of transliterating Farsi.
    """
    if isinstance(instance, Artist):
        # Prefer the dedicated English fields. Some legacy/admin-created artists
        # have no *_en value even though the primary name itself is entirely
        # English/Latin, so allow that primary name as the final safe fallback.
        # _strict_english_slug still rejects Persian/Arabic or mixed-script text.
        candidates = (instance.artistic_name_en, instance.name_en, instance.name)
    elif isinstance(instance, (Song, Album, Playlist, RecommendedPlaylist, EventPlaylist)):
        candidates = (instance.title_en,)
    elif isinstance(instance, (Genre, Mood, Tag, SubGenre)):
        candidates = (instance.name_en,)
    elif isinstance(instance, UserPlaylist):
        # User playlists do not have a parallel English field. Keep a slug only
        # when the stored title itself is already English/ASCII.
        candidates = (instance.title,)
    else:
        return None

    for candidate in candidates:
        slug = _strict_english_slug(candidate)
        if slug:
            return slug
    return ''


def _related_url_slug(instance):
    if instance is None:
        return ''
    value = _canonical_url_slug(instance)
    return value or ''


def _official_creator_uid(serializer):
    if not hasattr(serializer, '_official_creator_uid_value'):
        serializer._official_creator_uid_value = User.objects.filter(
            first_name='SedaBox |', last_name='صداباکس'
        ).values_list('unique_id', flat=True).first()
    return serializer._official_creator_uid_value


def _relation_items(obj, relation):
    cache = getattr(obj, '_serializer_relation_cache', None)
    if cache is None:
        cache = {}
        obj._serializer_relation_cache = cache
    if relation not in cache:
        prefetched = getattr(obj, '_prefetched_objects_cache', {}).get(relation)
        cache[relation] = list(prefetched if prefetched is not None else getattr(obj, relation).all())
    return cache[relation]


def _ensure_song_metrics(obj, request=None):
    needed = ('_play_count', '_likes_count', '_playlist_count', '_playlist_users_count', '_is_liked')
    if not all(hasattr(obj, name) for name in needed):
        user = getattr(request, 'user', None) if request is not None else None
        hydrate_song_metrics([obj], user if getattr(user, 'is_authenticated', False) else None)
    return obj


class SongMetricsListSerializer(serializers.ListSerializer):
    """Batch all song metrics once before child serialization."""
    def to_representation(self, data):
        iterable = data.all() if isinstance(data, BaseManager) else data
        items = list(iterable)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request is not None else None
        hydrate_song_metrics(items, user if getattr(user, 'is_authenticated', False) else None)
        return super().to_representation(items)


class ArtistMetricsListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        iterable = data.all() if isinstance(data, BaseManager) else data
        items = list(iterable)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request is not None else None
        hydrate_artist_metrics(items, user if getattr(user, 'is_authenticated', False) else None)
        return super().to_representation(items)


class AlbumMetricsListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        iterable = data.all() if isinstance(data, BaseManager) else data
        items = list(iterable)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request is not None else None
        hydrate_album_metrics(items, user if getattr(user, 'is_authenticated', False) else None)
        return super().to_representation(items)


class PlaylistMetricsListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        iterable = data.all() if isinstance(data, BaseManager) else data
        items = list(iterable)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request is not None else None
        hydrate_playlist_metrics(items, user if getattr(user, 'is_authenticated', False) else None)
        return super().to_representation(items)


class SongWithSimilarListSerializer(SongMetricsListSerializer):
    """Batch similar-song hydration for full song lists.

    Ranking IDs remain independently cached per source song, preserving the
    exact ranking contract, but all selected similar rows for the response are
    fetched/metric-hydrated together instead of one ORM query group per song.
    """
    def to_representation(self, data):
        iterable = data.all() if isinstance(data, BaseManager) else data
        items = list(iterable)
        request = self.context.get('request')
        if request and '/artist/' in request.path:
            return super().to_representation(items)
        if not items:
            return super().to_representation(items)

        def positive(name, default, maximum):
            try:
                return max(1, min(int(request.query_params.get(name, default)), maximum))
            except (AttributeError, TypeError, ValueError):
                return default

        page = positive('similar_page', 1, 1000)
        page_size = positive('similar_page_size', 6, 24)
        start = (page - 1) * page_size
        ranked = {song.pk: ranked_similar_song_ids(song) for song in items}
        wanted = []
        seen = set()
        for ids in ranked.values():
            for song_id in ids[start:start + page_size]:
                if song_id not in seen:
                    seen.add(song_id)
                    wanted.append(song_id)

        rows = Song.objects.filter(pk__in=wanted).select_related('artist', 'album').prefetch_related(
            'featured_artists', 'genres', 'moods', 'tags', 'sub_genres'
        )
        by_id = {song.pk: song for song in rows}
        related = [by_id[song_id] for song_id in wanted if song_id in by_id]
        user = getattr(request, 'user', None) if request is not None else None
        hydrate_song_metrics(related, user if getattr(user, 'is_authenticated', False) else None, False)

        for source in items:
            ids = ranked.get(source.pk, [])
            selected = ids[start:start + page_size]
            group = [by_id[song_id] for song_id in selected if song_id in by_id]
            next_link = None
            if request and start + page_size < len(ids):
                from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
                parsed = urlparse(absolute_api_url(request, request.get_full_path()))
                query = parse_qs(parsed.query)
                query.update(similar_page=[str(page + 1)], similar_page_size=[str(page_size)])
                next_link = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
            source._similar_payload = {
                'items': SongSummarySerializer(group, many=True, context=self.context).data,
                'total': len(ids),
                'page': page,
                'has_next': start + page_size < len(ids),
                'next': next_link,
            }
        return super().to_representation(items)


def _relative_day_label(days, request=None):
    language = get_request_language(request)
    if days == 0:
        return "Today" if language == "en" else "امروز"
    return f"{days} days ago" if language == "en" else f"{days} روز پیش"

def _resolve_source(instance, source):
    current = instance
    for part in source.split('.'):
        if current is None:
            return None
        current = getattr(current, part, None)
        # Reverse relations expose a RelatedManager which is technically
        # callable, but calling it directly requires Django's private
        # ``manager=`` keyword. It is an attribute container here, not a
        # zero-argument serializer source.
        if callable(current) and not isinstance(current, BaseManager):
            try:
                current = current()
            except TypeError:
                # Non-zero-argument callables are not safe serializer sources.
                return None
    return current


class LocalizedModelSerializer(serializers.ModelSerializer):
    """Localize translatable fields while preserving explicit fa/en values.

    Existing Farsi columns remain canonical. A request for English changes the
    normal field value to its ``*_en`` sibling and every translated field also
    exposes ``*_fa`` and ``*_en`` for clients that need both versions.
    """

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        language = get_request_language(request)
        request_path = str(getattr(request, 'path', '') or '')
        artist_panel = request_path.startswith('/api/artist/') or request_path.startswith('/artist/')
        model = getattr(getattr(self, 'Meta', None), 'model', None)

        if model is not None:
            model_fields = {field.name for field in model._meta.get_fields()}
            for english_field in sorted(name for name in model_fields if name.endswith('_en')):
                base_field = english_field[:-3]
                if base_field not in model_fields or base_field not in data:
                    continue
                fa_value = getattr(instance, base_field, None)
                en_value = getattr(instance, english_field, None)
                if en_value in (None, '', [], {}):
                    en_value = translate_generated_text(fa_value) if isinstance(fa_value, str) else fa_value
                data[f'{base_field}_fa'] = fa_value
                data[f'{base_field}_en'] = en_value
                # Artist editing must always receive the canonical Persian/base
                # value in the base field and the real English value separately.
                # Audience endpoints keep their existing request-language behavior.
                data[base_field] = (
                    fa_value
                    if artist_panel
                    else en_value if language == 'en' and en_value not in (None, '', [], {}) else fa_value
                )

        # Localize declared fields sourced from related objects, such as
        # ``artist_name = CharField(source='artist.name')``.
        for output_name, serializer_field in self.fields.items():
            source = getattr(serializer_field, 'source', None)
            if not source or source == '*' or output_name not in data:
                continue
            source_parts = source.split('.')
            if len(source_parts) == 1:
                parent = instance
                leaf = source_parts[0]
            else:
                parent = _resolve_source(instance, '.'.join(source_parts[:-1]))
                leaf = source_parts[-1]
            if parent is None or not hasattr(parent, f'{leaf}_en'):
                continue
            fa_value = getattr(parent, leaf, None)
            en_value = getattr(parent, f'{leaf}_en', None)
            if en_value in (None, '', [], {}):
                en_value = translate_generated_text(fa_value) if isinstance(fa_value, str) else fa_value
            data[f'{output_name}_fa'] = fa_value
            data[f'{output_name}_en'] = en_value
            data[output_name] = en_value if language == 'en' and en_value not in (None, '', [], {}) else fa_value

        canonical_url_slug = _canonical_url_slug(instance)
        if canonical_url_slug is not None:
            data['url_slug'] = canonical_url_slug

        # Server-generated playlists must never leak Farsi or legacy Finglish
        # into English responses when an old row has missing/bad English copy.
        # This correction is O(1) and performs no relation/database access.
        is_generated_playlist = isinstance(instance, RecommendedPlaylist) or (
            isinstance(instance, Playlist)
            and getattr(instance, 'created_by', None) == Playlist.CREATED_BY_SYSTEM
        )
        if language == 'en' and is_generated_playlist:
            for field_name in ('title', 'description'):
                if field_name not in data:
                    continue
                english_value = generated_playlist_english(instance, field_name)
                data[f'{field_name}_en'] = english_value
                data[field_name] = english_value

        return data


class SongSummarySerializer(LocalizedModelSerializer):
    """Compact song payload used by cards, queues and nested detail responses."""
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(source='artist.id', read_only=True)
    artist_unique_id = serializers.CharField(source='artist.unique_id', read_only=True)
    featured_artists = serializers.SerializerMethodField()
    album_title = serializers.CharField(source='album.title', read_only=True, allow_null=True)
    album_id = serializers.IntegerField(read_only=True, allow_null=True)
    stream_url = serializers.SerializerMethodField()
    preview_url = serializers.SerializerMethodField()
    is_preview = serializers.SerializerMethodField()
    preview_duration_seconds = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    genres = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    tag_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()
    sub_genre_names = serializers.SerializerMethodField()
    play_count = serializers.SerializerMethodField()
    genre_ids = serializers.SerializerMethodField()
    tag_ids = serializers.SerializerMethodField()
    mood_ids = serializers.SerializerMethodField()
    sub_genre_ids = serializers.SerializerMethodField()
    is_promoted = serializers.SerializerMethodField()

    class Meta:
        model = Song
        list_serializer_class = SongMetricsListSerializer
        fields = [
            'id', 'title', 'artist_id', 'artist_name', 'artist_unique_id', 'featured_artists',
            'album_id', 'album_title', 'cover_image', 'stream_url', 'preview_url', 'is_preview',
            'preview_duration_seconds', 'duration_seconds', 'is_liked', 'genres', 'genre_names', 'tag_names',
            'mood_names', 'sub_genre_names', 'play_count', 'genre_ids', 'tag_ids', 'mood_ids',
            'sub_genre_ids', 'is_promoted',
        ]

    def get_featured_artists(self, obj):
        request = self.context.get('request')
        return [
            {
                'id': a.id,
                'unique_id': a.unique_id,
                'url_slug': _related_url_slug(a),
                'name': localized_value(a, 'name', request),
                'name_fa': a.name,
                'name_en': a.name_en or a.name,
                'artistic_name': localized_value(a, 'artistic_name', request),
                'artistic_name_fa': a.artistic_name,
                'artistic_name_en': a.artistic_name_en or a.artistic_name,
            }
            for a in _relation_items(obj, 'featured_artists')
        ]

    def _items(self, obj, relation):
        return _relation_items(obj, relation)

    def get_genres(self, obj):
        request = self.context.get('request')
        return [
            {
                'id': genre.id,
                'name': localized_value(genre, 'name', request),
                'name_en': genre.name_en,
                'url_slug': _related_url_slug(genre),
            }
            for genre in self._items(obj, 'genres')
        ]

    def get_genre_names(self, obj): return [item['name'] for item in self.get_genres(obj)]
    def get_tag_names(self, obj): return [localized_value(x, 'name', self.context.get('request')) for x in self._items(obj, 'tags')]
    def get_mood_names(self, obj): return [localized_value(x, 'name', self.context.get('request')) for x in self._items(obj, 'moods')]
    def get_sub_genre_names(self, obj): return [localized_value(x, 'name', self.context.get('request')) for x in self._items(obj, 'sub_genres')]
    def get_genre_ids(self, obj): return [x.id for x in self._items(obj, 'genres')]
    def get_tag_ids(self, obj): return [x.id for x in self._items(obj, 'tags')]
    def get_mood_ids(self, obj): return [x.id for x in self._items(obj, 'moods')]
    def get_sub_genre_ids(self, obj): return [x.id for x in self._items(obj, 'sub_genres')]
    def get_is_promoted(self, obj): return bool(getattr(obj, '_is_admin_promoted', False))

    def get_is_liked(self, obj):
        request = self.context.get('request')
        _ensure_song_metrics(obj, request)
        return bool(getattr(obj, '_is_liked', False))

    def get_stream_url(self, obj): return _stream_wrapper(obj, self.context.get('request'))
    def get_preview_url(self, obj): return _preview_url(obj)
    def get_is_preview(self, obj):
        request = self.context.get('request')
        return bool((not request or not request.user.is_authenticated) and obj.preview_audio_url)
    def get_preview_duration_seconds(self, obj): return min(30, obj.duration_seconds or 30) if obj.preview_audio_url else 0
    def get_play_count(self, obj):
        _ensure_song_metrics(obj, self.context.get('request'))
        return int(obj.plays or 0) + int(getattr(obj, '_play_count', 0) or 0)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['artist_url_slug'] = _related_url_slug(getattr(instance, 'artist', None))
        data['album_url_slug'] = _related_url_slug(getattr(instance, 'album', None))
        data['cover_image'] = _signed_url(data.get('cover_image'))
        return data


class BannerAdSerializer(LocalizedModelSerializer):
    """Public serializer for banner ads returned to audience clients."""
    class Meta:
        model = BannerAd
        fields = ['id', 'title', 'title_en', 'image', 'navigate_link', 'view_count']
        read_only_fields = ['id', 'image', 'view_count']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if ret.get('image'):
            signed = generate_signed_r2_url(ret['image'])
            if signed:
                ret['image'] = signed
        return ret
    


class ArtistSummarySerializer(LocalizedModelSerializer):
    is_following = serializers.SerializerMethodField()
    followers_count = serializers.SerializerMethodField()
    followings_count = serializers.SerializerMethodField()
    monthly_listeners_count = serializers.SerializerMethodField()
    social_accounts = serializers.SerializerMethodField()

    class Meta:
        list_serializer_class = ArtistMetricsListSerializer
        model = Artist
        fields = [
            'id', 'name', 'artistic_name', 'unique_id', 'bio', 'profile_image',
            'banner_image', 'verified', 'followers_count', 'followings_count',
            'monthly_listeners_count', 'is_following', 'social_accounts',
        ]

    def get_followers_count(self, obj):
        return int(_metric(obj, '_followers_count', lambda: Follow.objects.filter(followed_artist=obj).count()))

    def get_followings_count(self, obj):
        return int(_metric(obj, '_followings_count', lambda: Follow.objects.filter(follower_artist=obj).count()))

    def get_monthly_listeners_count(self, obj):
        from django.utils import timezone
        from datetime import timedelta
        return int(_metric(obj, '_monthly_listeners_count', lambda: obj.monthly_listener_records.filter(
            updated_at__gte=timezone.now() - timedelta(days=28)
        ).values('user_id').distinct().count()))

    def get_is_following(self, obj):
        request = self.context.get('request')
        return bool(_metric(obj, '_is_following', lambda: request and request.user.is_authenticated and Follow.objects.filter(
            follower_user=request.user, followed_artist=obj
        ).exists()))

    def get_social_accounts(self, obj):
        links = getattr(obj, '_social_links', None)
        if links is None:
            links = getattr(obj, '_prefetched_objects_cache', {}).get('social_account_links')
        if links is None:
            links = obj.social_account_links.select_related('platform').all()
        return ArtistSocialAccountSerializer(links, many=True, context=self.context).data

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['profile_image'] = _signed_url(data.get('profile_image'))
        data['banner_image'] = _signed_url(data.get('banner_image'))
        return data



class AlbumSummarySerializer(LocalizedModelSerializer):
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(read_only=True)
    artist_unique_id = serializers.CharField(source='artist.unique_id', read_only=True)
    is_liked = serializers.SerializerMethodField()
    genres = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()
    sub_genre_names = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()

    class Meta:
        list_serializer_class = AlbumMetricsListSerializer
        model = Album
        fields = ['id', 'title', 'artist_id', 'artist_name', 'artist_unique_id', 'cover_image', 'is_liked', 'genres', 'genre_names', 'mood_names', 'sub_genre_names']

    def _songs(self, obj):
        songs = getattr(obj, '_card_songs', None)
        return list(songs if songs is not None else obj.songs.all())

    def _combined(self, obj, relation):
        request = self.context.get('request')
        values = {localized_value(x, 'name', request) for x in getattr(obj, relation).all()}
        for song in self._songs(obj):
            values.update(localized_value(x, 'name', request) for x in getattr(song, relation).all())
        return sorted(values)

    def get_genres(self, obj):
        request = self.context.get('request')
        genres = {genre.id: genre for genre in _relation_items(obj, 'genres')}
        for song in self._songs(obj):
            genres.update({genre.id: genre for genre in song.genres.all()})
        return sorted(
            ({'id': genre.id, 'name': localized_value(genre, 'name', request), 'url_slug': _related_url_slug(genre)} for genre in genres.values()),
            key=lambda item: item['name'],
        )

    def get_genre_names(self, obj): return [item['name'] for item in self.get_genres(obj)]
    def get_mood_names(self, obj): return self._combined(obj, 'moods')
    def get_sub_genre_names(self, obj): return self._combined(obj, 'sub_genres')
    def get_cover_image(self, obj):
        value = obj.cover_image
        if not value:
            first = next(iter(self._songs(obj)), None)
            value = getattr(first, 'cover_image', None)
        return _signed_url(value)
    def get_is_liked(self, obj):
        request = self.context.get('request')
        return bool(_metric(obj, '_is_liked', lambda: request and request.user.is_authenticated and AlbumLike.objects.filter(user=request.user, album=obj).exists()))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['artist_url_slug'] = _related_url_slug(getattr(instance, 'artist', None))
        return data


class PlaylistSummarySerializer(LocalizedModelSerializer):
    songs_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()
    top_three_song_covers = serializers.SerializerMethodField()
    genres = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='recommended')
    generated_by = serializers.ReadOnlyField(default='system')
    creator_unique_id = serializers.SerializerMethodField()

    class Meta:
        model = RecommendedPlaylist
        fields = [
            'id', 'unique_id', 'title', 'description', 'cover_image',
            'top_three_song_covers', 'songs_count', 'is_liked', 'genres', 'genre_names',
            'mood_names', 'type', 'generated_by', 'creator_unique_id',
        ]

    def _songs(self, obj):
        songs = getattr(obj, '_card_songs', None)
        if songs is None:
            songs = getattr(obj, '_detail_songs', None)
        return list(songs if songs is not None else obj.songs.all())

    def get_creator_unique_id(self, obj):
        value = getattr(obj, '_creator_unique_id', None)
        if value is not None:
            return value
        if not hasattr(self, '_creator_uid'):
            self._creator_uid = User.objects.filter(
                Q(unique_id='sedabox') | Q(first_name='SedaBox |', last_name='صداباکس')
            ).values_list('unique_id', flat=True).first() or 'sedabox'
        return self._creator_uid

    def get_genres(self, obj):
        request = self.context.get('request')
        genres = {genre.id: genre for song in self._songs(obj) for genre in song.genres.all()}
        return sorted(
            ({'id': genre.id, 'name': localized_value(genre, 'name', request), 'url_slug': _related_url_slug(genre)} for genre in genres.values()),
            key=lambda item: item['name'],
        )

    def get_genre_names(self, obj):
        return [item['name'] for item in self.get_genres(obj)]

    def get_mood_names(self, obj):
        return sorted({localized_value(m, 'name', self.context.get('request')) for song in self._songs(obj) for m in song.moods.all()})

    def get_top_three_song_covers(self, obj):
        song_map = {song.id: song for song in self._songs(obj)}
        ordered = [song_map[sid] for sid in (obj.song_order or []) if sid in song_map]
        ordered.extend(song for song in song_map.values() if song not in ordered)
        return [_signed_url(song.cover_image or getattr(song.album, 'cover_image', None)) for song in ordered[:3] if song.cover_image or getattr(song.album, 'cover_image', None)]

    def get_cover_image(self, obj):
        if obj.playlist_ref_id and getattr(obj.playlist_ref, 'cover_image', None):
            return _signed_url(obj.playlist_ref.cover_image)
        covers = self.get_top_three_song_covers(obj)
        return covers[0] if covers else None

    def get_songs_count(self, obj):
        return int(_metric(obj, '_songs_count', lambda: obj.songs.count()))

    def get_is_liked(self, obj):
        request = self.context.get('request')
        return bool(_metric(obj, '_is_liked', lambda: request and request.user.is_authenticated and obj.liked_by.filter(id=request.user.id).exists()))



class SimplePlaylistSerializer(LocalizedModelSerializer):
    songs_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    cover_image = serializers.SerializerMethodField()
    top_three_song_covers = serializers.SerializerMethodField()
    genres = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    mood_names = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='normal-playlist')
    generated_by = serializers.CharField(source='created_by', read_only=True)
    creator_unique_id = serializers.SerializerMethodField()

    class Meta:
        list_serializer_class = PlaylistMetricsListSerializer
        model = Playlist
        fields = [
            'id', 'unique_id', 'title', 'description', 'cover_image', 'top_three_song_covers',
            'songs_count', 'is_liked', 'likes_count', 'genres', 'genre_names', 'mood_names',
            'type', 'generated_by', 'creator_unique_id',
        ]

    def _songs(self, obj):
        songs = getattr(obj, '_card_songs', None)
        if songs is None:
            songs = getattr(obj, '_detail_songs', None)
        return list(songs if songs is not None else obj.songs.all())

    def get_likes_count(self, obj):
        return int(_metric(obj, '_likes_count', lambda: PlaylistLike.objects.filter(playlist=obj).count()))

    def get_creator_unique_id(self, obj):
        value = getattr(obj, '_creator_unique_id', None)
        if value is not None:
            return value
        if not hasattr(self, '_creator_uid'):
            self._creator_uid = User.objects.filter(first_name='SedaBox |', last_name='صداباکس').values_list('unique_id', flat=True).first()
        return self._creator_uid

    def get_genres(self, obj):
        request = self.context.get('request')
        return [
            {'id': genre.id, 'name': localized_value(genre, 'name', request), 'url_slug': _related_url_slug(genre)}
            for genre in _relation_items(obj, 'genres')
        ]

    def get_genre_names(self, obj): return [item['name'] for item in self.get_genres(obj)]
    def get_mood_names(self, obj): return [localized_value(m, 'name', self.context.get('request')) for m in _relation_items(obj, 'moods')]

    def get_top_three_song_covers(self, obj):
        return [_signed_url(song.cover_image or getattr(song.album, 'cover_image', None)) for song in self._songs(obj)[:3] if song.cover_image or getattr(song.album, 'cover_image', None)]

    def get_songs_count(self, obj):
        return int(_metric(obj, '_songs_count', lambda: obj.songs.count()))

    def get_is_liked(self, obj):
        request = self.context.get('request')
        return bool(_metric(obj, '_is_liked', lambda: request and request.user.is_authenticated and PlaylistLike.objects.filter(user=request.user, playlist=obj).exists()))

    def get_cover_image(self, obj):
        if obj.cover_image:
            return _signed_url(obj.cover_image)
        covers = self.get_top_three_song_covers(obj)
        return covers[0] if covers else None



class FollowableEntitySerializer(serializers.Serializer):
    """Unified serializer for both User and Artist in follow lists"""
    id = serializers.IntegerField()
    type = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    unique_id = serializers.SerializerMethodField()
    is_verified = serializers.SerializerMethodField()
    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()

    def get_type(self, obj):
        return 'artist' if isinstance(obj, Artist) else 'user'

    def get_unique_id(self, obj):
        # Return unique_id for both Artist and User when available
        try:
            return getattr(obj, 'unique_id', None)
        except Exception:
            return None

    def get_name(self, obj):
        if isinstance(obj, Artist):
            return localized_value(obj, 'name', self.context.get('request'))
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name if name else obj.phone_number

    def get_image(self, obj):
        if isinstance(obj, Artist):
            if obj.profile_image:
                signed = generate_signed_r2_url(obj.profile_image)
                return signed if signed else obj.profile_image
            return obj.profile_image
        return user_profile_image_url(obj, self.context.get('request'))

    def get_is_verified(self, obj):
        if isinstance(obj, Artist):
            return obj.verified
        return obj.is_verified

    def get_followers_count(self, obj):
        if hasattr(obj, '_followers_count'):
            return int(obj._followers_count or 0)
        if isinstance(obj, Artist):
            return Follow.objects.filter(followed_artist=obj).count()
        return Follow.objects.filter(followed_user=obj).count()

    def get_following_count(self, obj):
        if hasattr(obj, '_followings_count'):
            return int(obj._followings_count or 0)
        if isinstance(obj, Artist):
            return Follow.objects.filter(follower_artist=obj).count()
        return Follow.objects.filter(follower_user=obj).count()

    def get_is_following(self, obj):
        if hasattr(obj, '_is_following'):
            return bool(obj._is_following)
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return False
        if isinstance(obj, Artist):
            return Follow.objects.filter(follower_user=request.user, followed_artist=obj).exists()
        return Follow.objects.filter(follower_user=request.user, followed_user=obj).exists()


class FollowRequestSerializer(serializers.Serializer):
    user_id = serializers.IntegerField(required=False)
    artist_id = serializers.IntegerField(required=False)
    # Optional idempotent target state. Older clients may omit this field and
    # keep the legacy toggle behaviour, while newer clients can safely retry a
    # request without accidentally reversing the follow state.
    follow = serializers.BooleanField(required=False)

    def validate(self, data):
        if not data.get('user_id') and not data.get('artist_id'):
            raise serializers.ValidationError("Either user_id or artist_id must be provided.")
        if data.get('user_id') and data.get('artist_id'):
            raise serializers.ValidationError("Only one of user_id or artist_id should be provided.")
        return data


class NotificationSettingSerializer(LocalizedModelSerializer):
    class Meta:
        model = NotificationSetting
        fields = [
            'new_song_followed_artists', 'new_album_followed_artists', 
            'new_playlist', 'new_likes', 'new_follower', 'system_notifications'
        ]


class UserImageProfileSerializer(LocalizedModelSerializer):
    class Meta:
        model = UserImageProfile
        fields = ['id', 'image', 'status', 'created_at', 'updated_at']
        read_only_fields = ['id', 'status', 'created_at', 'updated_at']

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['image'] = public_media_url(
            self.context.get('request'),
            instance.image,
            version=instance.updated_at,
        )
        return data


class UserSerializer(LocalizedModelSerializer):
    is_premium = serializers.SerializerMethodField()
    premium_expires_at = serializers.SerializerMethodField()
    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    user_playlists_count = serializers.IntegerField(source='user_playlists.count', read_only=True)
    recently_played = serializers.SerializerMethodField()
    notification_setting = NotificationSettingSerializer(read_only=True)
    image_profile = UserImageProfileSerializer(read_only=True)
    followers = serializers.SerializerMethodField()
    following = serializers.SerializerMethodField()
    
    class Meta:
        model = get_user_model()
        fields = [
            'id', 'phone_number', 'unique_id', 'first_name', 'last_name', 'email',
            'roles', 'is_active', 'is_staff', 'date_joined',
            'followers_count', 'following_count', 'user_playlists_count', 
            'recently_played', 'notification_setting', 'image_profile', 'plan', 'is_premium',
            'premium_expires_at', 'stream_quality',
            'followers', 'following'
        ]
        read_only_fields = [
            'id', 'is_active', 'is_staff', 'date_joined', 
            'followers_count', 'following_count', 'user_playlists_count',
            'notification_setting', 'image_profile', 'followers', 'following', 'plan',
            'is_premium', 'premium_expires_at'
        ]

    def to_representation(self, instance):
        normalize_expired_premium(instance)
        return super().to_representation(instance)

    def get_is_premium(self, obj):
        return obj.plan == User.PLAN_PREMIUM

    def get_premium_expires_at(self, obj):
        expiry = premium_expires_at(obj)
        return expiry.isoformat() if expiry else None

    def get_followers_count(self, obj):
        return Follow.objects.filter(followed_user=obj).count()

    def get_following_count(self, obj):
        return Follow.objects.filter(follower_user=obj).count()

    def get_followers(self, obj):
        request = self.context.get('request')
        page, page_size = 1, 10
        if request:
            try:
                page = int(request.query_params.get('f_page', 1))
                page_size = int(request.query_params.get('f_page_size', 10))
            except (ValueError, TypeError): pass
        
        offset = (page - 1) * page_size
        qs = Follow.objects.filter(followed_user=obj).order_by('-created_at')
        total = qs.count()
        items = [f.follower_user or f.follower_artist for f in qs[offset:offset + page_size]]
        has_next = total > offset + page_size
        next_url = None
        if request and has_next:
            # prefer stable named route for profile lists
            try:
                base = reverse('user_profile')
            except Exception:
                base = request.path
            params = {k: str(v) for k, v in request.query_params.items()}
            params['f_page'] = str(page + 1)
            params['f_page_size'] = str(page_size)
            qs = urlencode(params)
            next_url = absolute_api_url(request, base + '?' + qs)

        return {
            'items': FollowableEntitySerializer(items, many=True, context=self.context).data,
            'total': total,
            'page': page,
            'has_next': has_next,
            'next': next_url,
        }

    def get_following(self, obj):
        request = self.context.get('request')
        page, page_size = 1, 10
        if request:
            try:
                page = int(request.query_params.get('fg_page', 1))
                page_size = int(request.query_params.get('fg_page_size', 10))
            except (ValueError, TypeError): pass
        
        offset = (page - 1) * page_size
        qs = Follow.objects.filter(follower_user=obj).order_by('-created_at')
        total = qs.count()
        items = [f.followed_user or f.followed_artist for f in qs[offset:offset + page_size]]
        has_next = total > offset + page_size
        next_url = None
        if request and has_next:
            try:
                base = reverse('user_profile')
            except Exception:
                base = request.path
            params = {k: str(v) for k, v in request.query_params.items()}
            params['fg_page'] = str(page + 1)
            params['fg_page_size'] = str(page_size)
            qs = urlencode(params)
            next_url = absolute_api_url(request, base + '?' + qs)

        return {
            'items': FollowableEntitySerializer(items, many=True, context=self.context).data,
            'total': total,
            'page': page,
            'has_next': has_next,
            'next': next_url,
        }

    def validate_stream_quality(self, value):
        user = self.instance
        if value == 'high' and user.plan != 'premium':
            raise serializers.ValidationError("High quality streaming is only available for premium users.")
        return value

    def update(self, instance, validated_data):
        # Handle nested notification_setting update
        notification_data = self.context['request'].data.get('notification_setting')
        if notification_data is not None:
            # Keep the legacy nested profile update path strict and atomic.  The
            # dedicated notification-settings endpoint is preferred by clients,
            # but malformed nested preferences must never be silently ignored.
            with transaction.atomic():
                notification_setting, _ = NotificationSetting.objects.get_or_create(user=instance)
                notification_setting = NotificationSetting.objects.select_for_update().get(
                    pk=notification_setting.pk
                )
                ns_serializer = NotificationSettingSerializer(
                    notification_setting,
                    data=notification_data,
                    partial=True,
                )
                ns_serializer.is_valid(raise_exception=True)
                ns_serializer.save()

        return super().update(instance, validated_data)

    def get_recently_played(self, obj):
        # Get unique songs recently played by this user, ordered by latest play
        from .models import Song
        from django.db.models import Max
        
        request = self.context.get('request')
        page = 1
        page_size = 10
        if request:
            try:
                page = int(request.query_params.get('rp_page', 1))
                page_size = int(request.query_params.get('rp_page_size', 10))
            except (ValueError, TypeError):
                pass

        offset = (page - 1) * page_size
        
        # Annotate each song with its latest play time for this user
        qs = Song.objects.filter(play_counts__user=obj).annotate(
            latest_play=Max('play_counts__created_at')
        ).order_by('-latest_play')
        
        total = qs.count()
        songs = qs[offset:offset + page_size]
        has_next = total > offset + page_size
        next_url = None
        if request and has_next:
            try:
                base = reverse('user_history_list')
            except Exception:
                base = request.path
            params = {k: str(v) for k, v in request.query_params.items()}
            params['rp_page'] = str(page + 1)
            params['rp_page_size'] = str(page_size)
            qs = urlencode(params)
            next_url = absolute_api_url(request, base + '?' + qs)

        return {
            'items': SongStreamSerializer(songs, many=True, context=self.context).data,
            'total': total,
            'page': page,
            'has_next': has_next,
            'next': next_url,
        }


class UserHistorySerializer(LocalizedModelSerializer):
    """Serializer for user history items with flattened content"""
    type = serializers.CharField(source='content_type')
    item = serializers.SerializerMethodField()

    class Meta:
        model = UserHistory
        fields = ['id', 'type', 'item', 'updated_at']

    def get_item(self, obj):
        request = self.context.get('request')
        # Handle user profile views
        if obj.content_type == UserHistory.TYPE_USER and obj.target_user:
            return UserSearchSummarySerializer(obj.target_user, context={'request': request}).data
        if obj.content_type == UserHistory.TYPE_SONG and obj.song:
            return SongSummarySerializer(obj.song, context={'request': request}).data
        elif obj.content_type == UserHistory.TYPE_ALBUM and obj.album:
            return AlbumSummarySerializer(obj.album, context={'request': request}).data
        elif obj.content_type == UserHistory.TYPE_PLAYLIST and obj.playlist:
            return SimplePlaylistSerializer(obj.playlist, context={'request': request}).data
        elif obj.content_type == UserHistory.TYPE_ARTIST and obj.artist:
            return ArtistSummarySerializer(obj.artist, context={'request': request}).data
        return None


class UserSearchSummarySerializer(LocalizedModelSerializer):
    """Lightweight serializer for users in search results."""
    followers_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    image_profile = serializers.SerializerMethodField()
    is_official = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='user')

    class Meta:
        model = User
        fields = ['id', 'unique_id', 'first_name', 'last_name', 'followers_count', 'is_following', 'image_profile', 'plan', 'is_official', 'type']

    def get_followers_count(self, obj):
        return int(_metric(obj, '_followers_count', lambda: Follow.objects.filter(followed_user=obj).count()))

    def get_image_profile(self, obj):
        try:
            profile = obj.image_profile
        except Exception:
            return None
        if profile.status != UserImageProfile.STATUS_PUBLISHED or not profile.image:
            return None
        return UserImageProfileSerializer(
            profile,
            context=self.context,
        ).data

    def get_is_following(self, obj):
        request = self.context.get('request')
        return bool(_metric(obj, '_is_following', lambda: request and request.user.is_authenticated and Follow.objects.filter(
            follower_user=request.user, followed_user=obj
        ).exists()))

    def get_is_official(self, obj):
        return (obj.unique_id or '').strip().casefold() == 'sedabox' or (
            (obj.first_name or '').strip().casefold().startswith('sedabox')
            and 'صداباکس' in (obj.last_name or '').replace(' ', '').replace('\u200c', '')
        )


class UserPublicProfileSerializer(LocalizedModelSerializer):
    """Serializer for a user's public profile"""
    followers_count = serializers.SerializerMethodField()
    following_count = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    is_yours = serializers.SerializerMethodField()
    image_profile = serializers.SerializerMethodField()
    user_playlists = serializers.SerializerMethodField()
    is_official = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'unique_id', 'first_name', 'last_name', 
            'followers_count', 'following_count', 'is_following',
            'is_yours',
            'image_profile', 'plan', 'user_playlists', 'is_official'
        ]

    def get_image_profile(self, obj):
        try:
            profile = obj.image_profile
        except Exception:
            return None
        if profile.status != UserImageProfile.STATUS_PUBLISHED or not profile.image:
            return None
        return UserImageProfileSerializer(
            profile,
            context=self.context,
        ).data

    def get_followers_count(self, obj):
        return Follow.objects.filter(followed_user=obj).count()

    def get_following_count(self, obj):
        return Follow.objects.filter(follower_user=obj).count()

    def get_is_following(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return Follow.objects.filter(follower_user=request.user, followed_user=obj).exists()
        return False

    def get_is_official(self, obj):
        return (obj.unique_id or '').strip().casefold() == 'sedabox' or (
            (obj.first_name or '').strip().casefold().startswith('sedabox')
            and 'صداباکس' in (obj.last_name or '').replace(' ', '').replace('\u200c', '')
        )

    def get_is_yours(self, obj):
        request = self.context.get('request')
        if not request:
            return False
        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        try:
            return int(user.id) == int(obj.id)
        except Exception:
            return False

    def get_user_playlists(self, obj):
        # Only public user playlists
        qs = obj.user_playlists.filter(public=True)
        return UserPlaylistSerializer(qs, many=True, context=self.context).data


class RegisterSerializer(LocalizedModelSerializer):
    password = serializers.CharField(write_only=True)
    # allow callers to request artist role at registration time (boolean)
    artist = serializers.BooleanField(write_only=True, required=False)
    artistPassword = serializers.CharField(write_only=True, required=False)

    playlists = serializers.JSONField(required=False)
    settings = serializers.JSONField(required=False)

    class Meta:
        model = User
        # Do NOT allow clients to set `roles` directly via this serializer.
        fields = ['phone_number', 'password', 'first_name', 'last_name', 'email', 'playlists', 'plan', 'settings', 'artist', 'artistPassword']

    def validate_phone_number(self, value):
        if User.objects.filter(phone_number=value).exists():
            raise serializers.ValidationError('این شماره تلفن قبلاً ثبت شده است.')
        return value

    def create(self, validated_data):
        password = validated_data.pop('password')
        artist_flag = validated_data.pop('artist', False)
        artist_password = validated_data.pop('artistPassword', None)

        create_kwargs = {}
        if artist_flag:
            create_kwargs['roles'] = [User.ROLE_AUDIENCE, User.ROLE_ARTIST]
        else:
            create_kwargs['roles'] = [User.ROLE_AUDIENCE]

        if artist_password:
            create_kwargs['artist_password'] = artist_password

        user = User.objects.create_user(password=password, **{**validated_data, **create_kwargs})
        return user


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        # attach user profile but exclude internal flags from the login response
        user_data = UserSerializer(self.user, context=self.context).data
        # remove `is_staff` so it doesn't appear in the token response
        user_data.pop('is_staff', None)
        data['user'] = user_data
        return data


class UploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    folder = serializers.CharField(required=False, allow_blank=True)
    filename = serializers.CharField(required=False, allow_blank=True)


# --- Auth related serializers ---
_AUTH_PHONE_ERROR = "شماره تلفن همراه معتبر وارد کنید."
_AUTH_OTP_ERROR = "کد تأیید چهاررقمی را کامل وارد کنید."


def _normalize_auth_phone(value):
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if digits.startswith('0098') and len(digits) == 13:
        digits = '0' + digits[4:]
    elif digits.startswith('98') and len(digits) == 12:
        digits = '0' + digits[2:]
    elif digits.startswith('9') and len(digits) == 10:
        digits = '0' + digits
    if len(digits) != 11 or not digits.startswith('09'):
        raise serializers.ValidationError(_AUTH_PHONE_ERROR, code='invalid_phone')
    return digits


def _validate_auth_otp(value):
    code = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if len(code) != 4:
        raise serializers.ValidationError(_AUTH_OTP_ERROR, code='invalid_otp_format')
    return code


class PhoneSerializer(serializers.Serializer):
    phone = serializers.CharField(trim_whitespace=True)

    def validate_phone(self, value):
        return _normalize_auth_phone(value)


class RegisterRequestSerializer(PhoneSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False, min_length=6)
    # Used only for privileged/admin flows. Normal registration never sets this.
    admin_login = serializers.BooleanField(required=False, default=False)
    # `artist` flag is taken from query params now; password used as artist password when artist=true


class VerifySerializer(PhoneSerializer):
    otp = serializers.CharField(trim_whitespace=True)

    def validate_otp(self, value):
        return _validate_auth_otp(value)


class LoginPasswordSerializer(PhoneSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    # Used only by the admin panel client to select the isolated admin credential.
    admin_login = serializers.BooleanField(required=False, default=False)


class LoginOtpRequestSerializer(PhoneSerializer):
    pass


class LoginOtpVerifySerializer(PhoneSerializer):
    otp = serializers.CharField(trim_whitespace=True)

    def validate_otp(self, value):
        return _validate_auth_otp(value)


class ForgotPasswordSerializer(PhoneSerializer):
    pass


class PasswordResetSerializer(PhoneSerializer):
    phone = serializers.CharField(required=True, trim_whitespace=True)
    otp = serializers.CharField(required=True, trim_whitespace=True)
    newPassword = serializers.CharField(write_only=True, trim_whitespace=False, min_length=6)
    resetToken = serializers.CharField(required=False, allow_blank=False, trim_whitespace=False)

    def validate_otp(self, value):
        return _validate_auth_otp(value)


class ArtistPasswordResetSerializer(PhoneSerializer):
    resetToken = serializers.CharField(trim_whitespace=False, allow_blank=False)
    newPassword = serializers.CharField(write_only=True, trim_whitespace=False, min_length=6)


class TokenRefreshRequestSerializer(serializers.Serializer):
    refreshToken = serializers.CharField(trim_whitespace=False, allow_blank=False)


class ArtistSocialAccountSerializer(LocalizedModelSerializer):
    platform_name = serializers.CharField(source='platform.name', read_only=True)
    platform_slug = serializers.CharField(source='platform.slug', read_only=True)
    platform_base_url = serializers.CharField(source='platform.base_url', read_only=True)

    class Meta:
        model = ArtistSocialAccount
        fields = [
            'id', 'platform', 'platform_name', 'platform_slug', 'platform_base_url',
            'username', 'url', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'platform_name', 'platform_slug', 'platform_base_url']
class LogoutSerializer(serializers.Serializer):
    refreshToken = serializers.CharField(trim_whitespace=False, allow_blank=False)


class ChangePasswordSerializer(serializers.Serializer):
    currentPassword = serializers.CharField(write_only=True, trim_whitespace=False)
    newPassword = serializers.CharField(write_only=True, trim_whitespace=False, min_length=6)

    def validate(self, attrs):
        if attrs.get('currentPassword') == attrs.get('newPassword'):
            raise serializers.ValidationError(
                {'newPassword': serializers.ErrorDetail(
                    'رمز عبور جدید باید با رمز عبور فعلی متفاوت باشد.',
                    code='password_unchanged',
                )}
            )
        return attrs


class ArtistFullListSerializer(serializers.ListSerializer):
    """Batch the full artist-list payload, including nested follow pages."""

    @staticmethod
    def _page(request, page_name, size_name, default_size=10):
        page, size = 1, default_size
        if request is not None:
            try:
                page = int(request.query_params.get(page_name, 1))
                size = int(request.query_params.get(size_name, default_size))
            except (TypeError, ValueError):
                pass
        return page, size, (page - 1) * size

    def to_representation(self, data):
        iterable = data.all() if isinstance(data, BaseManager) else data
        items = list(iterable)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request is not None else None
        _f_page, f_size, f_offset = self._page(request, 'f_page', 'f_page_size')
        _fg_page, fg_size, fg_offset = self._page(request, 'fg_page', 'fg_page_size')
        hydrate_artist_full_list(
            items,
            user if getattr(user, 'is_authenticated', False) else None,
            followers_offset=f_offset,
            followers_page_size=f_size,
            following_offset=fg_offset,
            following_page_size=fg_size,
        )
        return super().to_representation(items)


class ArtistSerializer(LocalizedModelSerializer):
    """Serializer for Artist model"""
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), 
        source='user', 
        required=False, 
        allow_null=True
    )
    followers_count = serializers.SerializerMethodField()
    followings_count = serializers.SerializerMethodField()
    monthly_listeners_count = serializers.SerializerMethodField()
    live_listeners = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    followers = serializers.SerializerMethodField()
    following = serializers.SerializerMethodField()
    social_accounts = ArtistSocialAccountSerializer(many=True, read_only=True, source='social_account_links')
    
    class Meta:
        model = Artist
        list_serializer_class = ArtistFullListSerializer
        fields = [
            'id', 'name', 'name_en', 'artistic_name', 'artistic_name_en', 'unique_id', 'user_id', 'bio', 'bio_en', 'profile_image', 'banner_image', 
            'email', 'city', 'city_en', 'date_of_birth', 'address', 'address_en', 'id_number',
            'verified', 'followers_count', 'followings_count', 
            'monthly_listeners_count', 'live_listeners', 'is_following', 'created_at',
            'followers', 'following', 'social_accounts'
        ]
        read_only_fields = [
            'id', 'created_at', 'followers_count', 'followings_count', 
            'monthly_listeners_count', 'live_listeners', 'is_following', 'followers', 'following', 'social_accounts'
        ]

    def get_followers_count(self, obj):
        return int(_metric(obj, '_followers_count', lambda: Follow.objects.filter(followed_artist=obj).count()))

    def get_followings_count(self, obj):
        return int(_metric(obj, '_followings_count', lambda: Follow.objects.filter(follower_artist=obj).count()))

    def get_followers(self, obj):
        request = self.context.get('request')
        page, page_size = 1, 10
        if request:
            try:
                page = int(request.query_params.get('f_page', 1))
                page_size = int(request.query_params.get('f_page_size', 10))
            except (ValueError, TypeError): pass
        
        offset = (page - 1) * page_size
        prepared = getattr(obj, '_followers_page_items', None)
        if prepared is not None:
            total = int(getattr(obj, '_followers_count', 0) or 0)
            items = prepared
        else:
            qs = Follow.objects.filter(followed_artist=obj).select_related(
                'follower_user', 'follower_artist'
            ).order_by('-created_at')
            total = qs.count()
            items = [f.follower_user or f.follower_artist for f in qs[offset:offset + page_size]]
        
        return {
            'items': FollowableEntitySerializer(items, many=True, context=self.context).data,
            'total': total,
            'page': page,
            'has_next': total > offset + page_size
        }

    def get_following(self, obj):
        request = self.context.get('request')
        page, page_size = 1, 10
        if request:
            try:
                page = int(request.query_params.get('fg_page', 1))
                page_size = int(request.query_params.get('fg_page_size', 10))
            except (ValueError, TypeError): pass
        
        offset = (page - 1) * page_size
        prepared = getattr(obj, '_following_page_items', None)
        if prepared is not None:
            total = int(getattr(obj, '_followings_count', 0) or 0)
            items = prepared
        else:
            qs = Follow.objects.filter(follower_artist=obj).select_related(
                'followed_user', 'followed_artist'
            ).order_by('-created_at')
            total = qs.count()
            items = [f.followed_user or f.followed_artist for f in qs[offset:offset + page_size]]
        
        return {
            'items': FollowableEntitySerializer(items, many=True, context=self.context).data,
            'total': total,
            'page': page,
            'has_next': total > offset + page_size
        }

    def get_live_listeners(self, obj):
        value = getattr(obj, '_live_listeners_count', None)
        return int(value if value is not None else obj.live_listeners)

    def get_monthly_listeners_count(self, obj):
        from django.utils import timezone
        from datetime import timedelta
        return int(_metric(obj, '_monthly_listeners_count', lambda: obj.monthly_listener_records.filter(
            updated_at__gte=timezone.now() - timedelta(days=28)
        ).count()))

    def get_is_following(self, obj):
        request = self.context.get('request')
        return bool(_metric(obj, '_is_following', lambda: request and request.user.is_authenticated and Follow.objects.filter(
            follower_user=request.user, followed_artist=obj
        ).exists()))

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        for field in ['profile_image', 'banner_image']:
            if ret.get(field):
                signed = generate_signed_r2_url(ret[field])
                if signed:
                    ret[field] = signed
        return ret


class PopularArtistSerializer(ArtistSummarySerializer):
    total_plays = serializers.IntegerField(read_only=True)
    total_likes = serializers.IntegerField(read_only=True)
    total_playlist_adds = serializers.IntegerField(read_only=True)
    score = serializers.IntegerField(read_only=True)

    class Meta(ArtistSummarySerializer.Meta):
        fields = ArtistSummarySerializer.Meta.fields + [
            'total_plays', 'total_likes', 'total_playlist_adds', 'score',
        ]



class ArtistAuthR2ImageField(serializers.ImageField):
    """Accept an uploaded image but represent stored verification media with a signed R2 URL."""

    def to_representation(self, value):
        if not value:
            return None
        raw = str(getattr(value, 'name', '') or '').strip()
        if not raw:
            return None
        if r2_object_key(raw, allow_key=False):
            return generate_signed_r2_url(
                raw, expiration=getattr(settings, 'ARTIST_R2_SIGNED_URL_TTL', 3600)
            ) or raw
        # Legacy local-media submission: keep it viewable while migration is pending.
        request = self.context.get('request') if hasattr(self, 'context') else None
        return public_media_url(request, value) or raw


class ArtistAuthSerializer(LocalizedModelSerializer):
    """Serializer for ArtistAuth verification submissions backed by private R2 media."""
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), source='user', required=False, allow_null=True
    )
    national_id_image = ArtistAuthR2ImageField(required=True)
    profile_image = ArtistAuthR2ImageField(required=False, allow_null=True)
    artist_claimed = serializers.PrimaryKeyRelatedField(
        queryset=Artist.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = ArtistAuth
        fields = [
            'id', 'user_id', 'auth_type', 'artist_claimed',
            'first_name', 'first_name_en', 'last_name', 'last_name_en',
            'stage_name', 'stage_name_en', 'birth_date', 'national_id',
            'phone_number', 'email', 'city', 'address',
            'biography', 'biography_en', 'profile_image', 'national_id_image',
            'status', 'is_verified', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'status', 'is_verified', 'created_at', 'updated_at']

    @staticmethod
    def _upload_image(upload, *, user, kind):
        if not upload:
            return None
        extension = os.path.splitext(str(getattr(upload, 'name', '') or ''))[1].lower()
        if extension not in {'.jpg', '.jpeg', '.png'}:
            extension = '.jpg'
        user_part = getattr(user, 'unique_id', None) or getattr(user, 'pk', None) or 'unknown'
        filename = f"u{user_part}-{kind}-{uuid.uuid4().hex[:12]}{extension}"
        folder = 'artist-auth/profiles' if kind == 'profile' else 'artist-auth/national-ids'
        url, _ = upload_file_to_r2(
            upload, folder=folder, custom_filename=filename, check_existing=False
        )
        return url

    def _save_with_r2_media(self, instance, validated_data):
        request = self.context.get('request')
        user = validated_data.get('user') or getattr(instance, 'user', None)
        if request and getattr(request, 'user', None) and request.user.is_authenticated:
            user = request.user
            validated_data['user'] = request.user

        supplied_profile = 'profile_image' in validated_data
        supplied_national = 'national_id_image' in validated_data
        profile_upload = validated_data.pop('profile_image', None) if supplied_profile else None
        national_upload = validated_data.pop('national_id_image', None) if supplied_national else None

        old_profile = str(getattr(getattr(instance, 'profile_image', None), 'name', '') or '') if instance else ''
        old_national = str(getattr(getattr(instance, 'national_id_image', None), 'name', '') or '') if instance else ''
        uploaded = {}
        try:
            if profile_upload:
                uploaded['profile_image'] = self._upload_image(profile_upload, user=user, kind='profile')
            elif supplied_profile:
                uploaded['profile_image'] = None
            if national_upload:
                uploaded['national_id_image'] = self._upload_image(national_upload, user=user, kind='national-id')

            with transaction.atomic():
                if instance is None:
                    instance = super().create(validated_data)
                else:
                    instance = super().update(instance, validated_data)

                changed_fields = []
                for field, value in uploaded.items():
                    setattr(instance, field, value)
                    changed_fields.append(field)
                if changed_fields:
                    instance.save(update_fields=changed_fields)
        except Exception:
            cleanup_r2_urls([value for value in uploaded.values() if value])
            raise

        replaced = []
        if 'profile_image' in uploaded and old_profile and old_profile != uploaded.get('profile_image'):
            if r2_object_key(old_profile, allow_key=False):
                replaced.append(old_profile)
        if 'national_id_image' in uploaded and old_national and old_national != uploaded.get('national_id_image'):
            if r2_object_key(old_national, allow_key=False):
                replaced.append(old_national)
        cleanup_r2_urls(replaced)
        return instance

    def create(self, validated_data):
        return self._save_with_r2_media(None, validated_data)

    def update(self, instance, validated_data):
        return self._save_with_r2_media(instance, validated_data)

    def validate(self, data):
        auth_type = data.get('auth_type') or getattr(self.instance, 'auth_type', None)
        artist_claimed = data.get('artist_claimed') or getattr(self.instance, 'artist_claimed', None)
        if auth_type == ArtistAuth.AUTH_EXISTING and not artist_claimed:
            raise serializers.ValidationError({
                'artist_claimed': 'برای احراز هویت هنرمند موجود، انتخاب پروفایل هنرمند الزامی است.'
            })

        # Fresh onboarding must capture real values in both supported languages.
        if auth_type == ArtistAuth.AUTH_FRESH and self.instance is None:
            required = ('first_name_en', 'last_name_en', 'stage_name_en')
            missing = {
                field: 'وارد کردن این مقدار انگلیسی برای ثبت هنرمند جدید الزامی است.'
                for field in required
                if not str(data.get(field) or '').strip()
            }
            if missing:
                raise serializers.ValidationError(missing)
        return data


class AlbumSerializer(LocalizedModelSerializer):
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(read_only=True)
    artist_unique_id = serializers.CharField(source='artist.unique_id', read_only=True)
    likes_count = serializers.SerializerMethodField()
    songs_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    genre_ids_write = serializers.PrimaryKeyRelatedField(queryset=Genre.objects.all(), many=True, source='genres', required=False, write_only=True)
    sub_genre_ids_write = serializers.PrimaryKeyRelatedField(queryset=SubGenre.objects.all(), many=True, source='sub_genres', required=False, write_only=True)
    mood_ids_write = serializers.PrimaryKeyRelatedField(queryset=Mood.objects.all(), many=True, source='moods', required=False, write_only=True)
    genre_ids = serializers.SerializerMethodField()
    sub_genre_ids = serializers.SerializerMethodField()
    mood_ids = serializers.SerializerMethodField()
    genre_items = serializers.SerializerMethodField()
    sub_genre_items = serializers.SerializerMethodField()
    mood_items = serializers.SerializerMethodField()
    songs = serializers.SerializerMethodField()
    song_genre_names = serializers.SerializerMethodField()
    song_mood_names = serializers.SerializerMethodField()

    class Meta:
        model = Album
        list_serializer_class = AlbumMetricsListSerializer
        fields = ['id', 'title', 'title_en', 'artist_id', 'artist_name', 'artist_unique_id', 'cover_image', 'release_date',
                  'description', 'description_en', 'created_at', 'likes_count', 'songs_count', 'is_liked', 'genre_ids_write', 'sub_genre_ids_write',
                  'mood_ids_write', 'genre_ids', 'sub_genre_ids', 'mood_ids', 'genre_items', 'sub_genre_items', 'mood_items', 'songs', 'song_genre_names', 'song_mood_names']
        read_only_fields = ['id', 'created_at', 'likes_count', 'is_liked']

    def get_likes_count(self, obj): return int(_metric(obj, '_likes_count', lambda: AlbumLike.objects.filter(album=obj).count()))
    def get_songs_count(self, obj): return len(self._songs(obj))
    def get_is_liked(self, obj):
        request = self.context.get('request')
        return bool(_metric(obj, '_is_liked', lambda: request and request.user.is_authenticated and AlbumLike.objects.filter(user=request.user, album=obj).exists()))
    def get_genre_ids(self, obj): return [localized_value(x, 'name', self.context.get('request')) for x in _relation_items(obj, 'genres')]
    def get_sub_genre_ids(self, obj): return [localized_value(x, 'name', self.context.get('request')) for x in _relation_items(obj, 'sub_genres')]
    def get_mood_ids(self, obj): return [localized_value(x, 'name', self.context.get('request')) for x in _relation_items(obj, 'moods')]
    def _taxonomy_items(self, values):
        request = self.context.get('request')
        return [{'id': item.id, 'title': localized_value(item, 'name', request), 'name': item.name, 'name_en': item.name_en} for item in values.all()]
    def get_genre_items(self, obj): return self._taxonomy_items(obj.genres)
    def get_sub_genre_items(self, obj): return self._taxonomy_items(obj.sub_genres)
    def get_mood_items(self, obj): return self._taxonomy_items(obj.moods)
    def _songs(self, obj):
        songs = getattr(obj, '_detail_songs', None)
        values = list(songs if songs is not None else obj.songs.all())
        return sorted(values, key=lambda song: (song.album_disc_number or 1, song.album_track_number or song.id, song.id))
    def get_songs(self, obj): return SongStreamSerializer(self._songs(obj), many=True, context=self.context).data
    def get_song_genre_names(self, obj): return sorted({localized_value(g, 'name', self.context.get('request')) for s in self._songs(obj) for g in s.genres.all()})
    def get_song_mood_names(self, obj): return sorted({localized_value(m, 'name', self.context.get('request')) for s in self._songs(obj) for m in s.moods.all()})
    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['artist_url_slug'] = _related_url_slug(getattr(instance, 'artist', None))
        value = data.get('cover_image') or next((s.cover_image for s in self._songs(instance) if s.cover_image), None)
        data['cover_image'] = _signed_url(value)
        return data


class PopularAlbumSerializer(AlbumSummarySerializer):
    total_song_plays = serializers.IntegerField(read_only=True)
    total_song_likes = serializers.IntegerField(read_only=True)
    album_likes = serializers.IntegerField(read_only=True)
    total_playlist_adds = serializers.IntegerField(read_only=True)
    score = serializers.IntegerField(read_only=True)
    top_song_covers = serializers.SerializerMethodField()

    class Meta(AlbumSummarySerializer.Meta):
        fields = AlbumSummarySerializer.Meta.fields + [
            'total_song_plays', 'total_song_likes', 'album_likes',
            'total_playlist_adds', 'score', 'top_song_covers',
        ]

    def get_top_song_covers(self, obj):
        return [_signed_url(song.cover_image) for song in self._songs(obj)[:3] if song.cover_image]



class GenreSerializer(LocalizedModelSerializer):
    """Serializer for Genre model"""
    title = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Genre
        fields = ['id', 'name', 'name_en', 'title', 'slug']
        read_only_fields = ['id']


class MoodSerializer(LocalizedModelSerializer):
    """Serializer for Mood model"""
    class Meta:
        model = Mood
        fields = ['id', 'name', 'name_en', 'slug']
        read_only_fields = ['id']


class TagSerializer(LocalizedModelSerializer):
    """Serializer for Tag model"""
    class Meta:
        model = Tag
        fields = ['id', 'name', 'name_en', 'slug']
        read_only_fields = ['id']


class SubGenreSerializer(LocalizedModelSerializer):
    """Serializer for SubGenre model"""
    parent_genre_name = serializers.CharField(source='parent_genre.name', read_only=True, allow_null=True)
    
    class Meta:
        model = SubGenre
        fields = ['id', 'name', 'name_en', 'slug', 'parent_genre', 'parent_genre_name']
        read_only_fields = ['id']


class SlimGenreSerializer(LocalizedModelSerializer):
    title = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Genre
        fields = ['id', 'title']


class SlimMoodSerializer(LocalizedModelSerializer):
    title = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Mood
        fields = ['id', 'title']


class SlimTagSerializer(LocalizedModelSerializer):
    title = serializers.CharField(source='name', read_only=True)

    class Meta:
        model = Tag
        fields = ['id', 'title']


class SongSerializer(LocalizedModelSerializer):
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(read_only=True)
    artist_unique_id = serializers.CharField(source='artist.unique_id', read_only=True)
    album_id = serializers.IntegerField(read_only=True, allow_null=True)
    album_title = serializers.CharField(source='album.title', read_only=True, allow_null=True)
    uploader_phone = serializers.CharField(source='uploader.phone_number', read_only=True, allow_null=True)
    uploader_unique_id = serializers.CharField(source='uploader.unique_id', read_only=True, allow_null=True)
    duration_display = serializers.ReadOnlyField()
    display_title = serializers.ReadOnlyField()
    stream_url = serializers.SerializerMethodField()
    preview_url = serializers.SerializerMethodField()
    is_preview = serializers.SerializerMethodField()
    preview_duration_seconds = serializers.SerializerMethodField()
    plays = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    added_to_playlists_count = serializers.SerializerMethodField()
    added_to_playlist = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    genre_ids_write = serializers.PrimaryKeyRelatedField(queryset=Genre.objects.all(), many=True, source='genres', required=False, write_only=True)
    sub_genre_ids_write = serializers.PrimaryKeyRelatedField(queryset=SubGenre.objects.all(), many=True, source='sub_genres', required=False, write_only=True)
    mood_ids_write = serializers.PrimaryKeyRelatedField(queryset=Mood.objects.all(), many=True, source='moods', required=False, write_only=True)
    tag_ids_write = serializers.PrimaryKeyRelatedField(queryset=Tag.objects.all(), many=True, source='tags', required=False, write_only=True)
    genre_ids = serializers.SerializerMethodField()
    sub_genre_ids = serializers.SerializerMethodField()
    mood_ids = serializers.SerializerMethodField()
    tag_ids = serializers.SerializerMethodField()
    featured_artists = serializers.SerializerMethodField()
    featured_artist_ids = serializers.PrimaryKeyRelatedField(queryset=Artist.objects.all(), many=True, source='featured_artists', required=False, write_only=True)
    genres = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    similar_songs = serializers.SerializerMethodField()
    is_promoted = serializers.SerializerMethodField()

    class Meta:
        model = Song
        list_serializer_class = SongWithSimilarListSerializer
        fields = ['id', 'title', 'title_en', 'artist_id', 'artist_name', 'artist_unique_id', 'featured_artists', 'featured_artist_ids',
                  'album', 'album_id', 'album_title', 'is_single', 'album_disc_number', 'album_track_number', 'stream_url', 'preview_url', 'is_preview',
                  'preview_duration_seconds', 'audio_file', 'converted_audio_url', 'cover_image', 'original_format',
                  'duration_seconds', 'duration_display', 'plays', 'likes_count', 'added_to_playlists_count',
                  'added_to_playlist', 'is_liked', 'status', 'release_date', 'language', 'genre_ids', 'sub_genre_ids',
                  'mood_ids', 'tag_ids', 'genres', 'genre_names', 'description', 'description_en', 'lyrics', 'lyrics_en', 'tempo', 'energy', 'danceability', 'valence',
                  'acousticness', 'instrumentalness', 'live_performed', 'speechiness', 'label', 'label_en', 'producers', 'producers_en',
                  'composers', 'composers_en', 'lyricists', 'lyricists_en', 'credits', 'credits_en', 'uploader', 'uploader_phone', 'uploader_unique_id', 'created_at',
                  'updated_at', 'display_title', 'similar_songs', 'is_promoted', 'genre_ids_write', 'sub_genre_ids_write',
                  'mood_ids_write', 'tag_ids_write']
        read_only_fields = ['id', 'plays', 'likes_count', 'added_to_playlists_count', 'added_to_playlist', 'is_liked',
                            'created_at', 'updated_at', 'duration_display', 'display_title']

    def get_featured_artists(self, obj):
        request = self.context.get('request')
        return [
            {
                'id': a.id,
                'unique_id': a.unique_id,
                'url_slug': _related_url_slug(a),
                'name': localized_value(a, 'name', request),
                'name_fa': a.name,
                'name_en': a.name_en or a.name,
                'artistic_name': localized_value(a, 'artistic_name', request),
                'artistic_name_fa': a.artistic_name,
                'artistic_name_en': a.artistic_name_en or a.artistic_name,
            }
            for a in _relation_items(obj, 'featured_artists')
        ]
    def get_genres(self, obj):
        request = self.context.get('request')
        return [
            {'id': genre.id, 'name': localized_value(genre, 'name', request), 'url_slug': _related_url_slug(genre)}
            for genre in _relation_items(obj, 'genres')
        ]
    def get_genre_names(self, obj): return [item['name'] for item in self.get_genres(obj)]
    def get_is_promoted(self, obj): return bool(getattr(obj, '_is_admin_promoted', False))
    def get_genre_ids(self, obj): return [{'id': x.id, 'title': localized_value(x, 'name', self.context.get('request'))} for x in _relation_items(obj, 'genres')]
    def get_sub_genre_ids(self, obj): return [{'id': x.id, 'title': localized_value(x, 'name', self.context.get('request'))} for x in _relation_items(obj, 'sub_genres')]
    def get_mood_ids(self, obj): return [{'id': x.id, 'title': localized_value(x, 'name', self.context.get('request'))} for x in _relation_items(obj, 'moods')]
    def get_tag_ids(self, obj): return [{'id': x.id, 'title': localized_value(x, 'name', self.context.get('request'))} for x in _relation_items(obj, 'tags')]
    def get_plays(self, obj):
        _ensure_song_metrics(obj, self.context.get('request'))
        return int(obj.plays or 0) + int(getattr(obj, '_play_count', 0) or 0)
    def get_likes_count(self, obj):
        _ensure_song_metrics(obj, self.context.get('request'))
        return int(getattr(obj, '_likes_count', 0) or 0)
    def get_added_to_playlists_count(self, obj):
        _ensure_song_metrics(obj, self.context.get('request'))
        return int(getattr(obj, '_playlist_count', 0) or 0)
    def get_added_to_playlist(self, obj):
        _ensure_song_metrics(obj, self.context.get('request'))
        return int(getattr(obj, '_playlist_users_count', 0) or 0)
    def get_is_liked(self, obj):
        request = self.context.get('request')
        _ensure_song_metrics(obj, request)
        return bool(getattr(obj, '_is_liked', False))
    def get_stream_url(self, obj): return _stream_wrapper(obj, self.context.get('request'))
    def get_preview_url(self, obj): return _preview_url(obj)
    def get_is_preview(self, obj):
        request = self.context.get('request')
        return bool((not request or not request.user.is_authenticated) and obj.preview_audio_url)
    def get_preview_duration_seconds(self, obj): return min(30, obj.duration_seconds or 30) if obj.preview_audio_url else 0

    def get_similar_songs(self, obj):
        request = self.context.get('request')
        if request and '/artist/' in request.path:
            return None
        prepared = getattr(obj, '_similar_payload', None)
        if prepared is not None:
            return prepared

        def positive(name, default, maximum):
            try:
                return max(1, min(int(request.query_params.get(name, default)), maximum))
            except (AttributeError, TypeError, ValueError):
                return default

        page = positive('similar_page', 1, 1000)
        page_size = positive('similar_page_size', 6, 24)
        ranked_ids = ranked_similar_song_ids(obj)
        start = (page - 1) * page_size
        selected = ranked_ids[start:start + page_size]
        rows = Song.objects.filter(pk__in=selected).select_related('artist', 'album').prefetch_related(
            'featured_artists', 'genres', 'moods', 'tags', 'sub_genres'
        )
        by_id = {song.pk: song for song in rows}
        items = [by_id[pk] for pk in selected if pk in by_id]
        hydrate_song_metrics(items, getattr(request, 'user', None), False)
        next_link = None
        if request and start + page_size < len(ranked_ids):
            from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
            parsed = urlparse(absolute_api_url(request, request.get_full_path()))
            query = parse_qs(parsed.query)
            query.update(similar_page=[str(page + 1)], similar_page_size=[str(page_size)])
            next_link = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))
        return {
            'items': SongSummarySerializer(items, many=True, context=self.context).data,
            'total': len(ranked_ids),
            'page': page,
            'has_next': start + page_size < len(ranked_ids),
            'next': next_link,
        }

    def to_representation(self, instance):
        data=super().to_representation(instance)
        data['artist_url_slug'] = _related_url_slug(getattr(instance, 'artist', None))
        data['album_url_slug'] = _related_url_slug(getattr(instance, 'album', None))
        data['cover_image']=_signed_url(data.get('cover_image'))
        request=self.context.get('request')
        if not request or not request.user.is_authenticated or not request.user.is_staff:
            data.pop('audio_file',None); data.pop('converted_audio_url',None)
        else:
            data['audio_file']=_signed_url(data.get('audio_file'))
            data['converted_audio_url']=_signed_url(data.get('converted_audio_url'))
        return data


class SongUploadSerializer(serializers.Serializer):
    """Serializer for uploading songs with audio file"""
    # File uploads
    audio_file = serializers.FileField(required=True, help_text="Audio file (mp3 or wav)")
    cover_image = serializers.ImageField(required=False, allow_null=True, help_text="Cover image")
    
    # Basic info
    title = serializers.CharField(max_length=400, required=True)
    title_en = serializers.CharField(max_length=400, required=False, allow_blank=True, default="")
    artist_id = serializers.IntegerField(required=True, help_text="Artist ID")
    featured_artist_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        default=list,
        help_text="List of artist IDs featured on this song"
    )
    album_id = serializers.IntegerField(required=False, allow_null=True)
    is_single = serializers.BooleanField(default=False)
    
    # Metadata
    release_date = serializers.DateField(required=False, allow_null=True)
    language = serializers.CharField(max_length=10, default="fa")
    description = serializers.CharField(required=False, allow_blank=True, default="")
    description_en = serializers.CharField(required=False, allow_blank=True, default="")
    lyrics = serializers.CharField(required=False, allow_blank=True, default="")
    lyrics_en = serializers.CharField(required=False, allow_blank=True, default="")
    
    # Classification
    genre_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        default=list
    )
    sub_genre_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        default=list
    )
    mood_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        default=list
    )
    tag_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        allow_empty=True,
        default=list
    )
    
    # Audio features
    tempo = serializers.IntegerField(required=False, allow_null=True)
    energy = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    danceability = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    valence = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    acousticness = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    instrumentalness = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    speechiness = serializers.IntegerField(required=False, allow_null=True, min_value=0, max_value=100)
    live_performed = serializers.BooleanField(default=False)
    
    # Credits
    label = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    label_en = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    producers = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        allow_empty=True,
        default=list
    )
    producers_en = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        allow_empty=True,
        default=list
    )
    composers = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        allow_empty=True,
        default=list
    )
    composers_en = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        allow_empty=True,
        default=list
    )
    lyricists = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        allow_empty=True,
        default=list
    )
    lyricists_en = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        allow_empty=True,
        default=list
    )
    credits = serializers.CharField(required=False, allow_blank=True, default="")
    credits_en = serializers.CharField(required=False, allow_blank=True, default="")
    
    def validate_audio_file(self, value):
        """Validate audio file format"""
        valid_extensions = ['.mp3', '.wav']
        ext = value.name.lower()[value.name.rfind('.'):]
        if ext not in valid_extensions:
            raise serializers.ValidationError("فقط فایل‌های صوتی MP3 و WAV پشتیبانی می‌شوند.")
        return value

    def validate_featured_artists(self, value):
        """Reject empty/blank entries coming from front-end.

        The frontend sometimes sends ["\""] when no artists are entered. We
        accept the list but filter out invalid items and return an empty list if
        nothing meaningful remains. The view logic already tolerates an empty
        list and the model default will store `[]`.
        """
        if not value:
            return []
        cleaned = [str(v) for v in value if v and str(v).strip()]
        return cleaned
    def validate_album_id(self, value):
        """Validate album exists if provided"""
        if value and not Album.objects.filter(id=value).exists():
            raise serializers.ValidationError("آلبوم موردنظر پیدا نشد.")
        return value



def _prepare_playlist_song_order(playlists):
    items = [item for item in playlists if getattr(item, 'pk', None)]
    if not items:
        return items
    through = Playlist.songs.through
    order_map = {item.pk: [] for item in items}
    for playlist_id, song_id in (
        through.objects.filter(playlist_id__in=order_map)
        .order_by('playlist_id', 'pk')
        .values_list('playlist_id', 'song_id')
    ):
        order_map[int(playlist_id)].append(int(song_id))
    for item in items:
        item._ordered_song_ids = order_map.get(item.pk, [])
    return items


class OrderedPlaylistListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        iterable = data.all() if isinstance(data, BaseManager) else data
        items = list(iterable)
        _prepare_playlist_song_order(items)
        request = self.context.get('request')
        user = getattr(request, 'user', None) if request is not None else None
        hydrate_playlist_metrics(items, user if getattr(user, 'is_authenticated', False) else None)
        return super().to_representation(items)


def _ordered_playlist_songs(playlist):
    """Return official playlist songs in the order persisted by the M2M rows."""
    ordered_ids = getattr(playlist, '_ordered_song_ids', None)
    if ordered_ids is None:
        through = Playlist.songs.through
        ordered_ids = list(
            through.objects.filter(playlist_id=playlist.pk)
            .order_by('pk')
            .values_list('song_id', flat=True)
        )
    if not ordered_ids:
        return []
    song_map = {song.id: song for song in playlist.songs.all()}
    return [song_map[song_id] for song_id in ordered_ids if song_id in song_map]


class PlaylistSerializer(LocalizedModelSerializer):
    genres = GenreSerializer(many=True, read_only=True)
    moods = MoodSerializer(many=True, read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    songs = serializers.SerializerMethodField()
    songs_count = serializers.SerializerMethodField()
    genre_ids = serializers.PrimaryKeyRelatedField(queryset=Genre.objects.all(), many=True, source='genres', required=False)
    mood_ids = serializers.PrimaryKeyRelatedField(queryset=Mood.objects.all(), many=True, source='moods', required=False)
    tag_ids = serializers.PrimaryKeyRelatedField(queryset=Tag.objects.all(), many=True, source='tags', required=False)
    song_ids = serializers.PrimaryKeyRelatedField(queryset=Song.objects.all(), many=True, source='songs', required=False)
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    generated_by = serializers.CharField(source='created_by', read_only=True)
    creator_unique_id = serializers.SerializerMethodField()

    class Meta:
        model = Playlist
        list_serializer_class = OrderedPlaylistListSerializer
        fields = ['id','unique_id','title','title_en','description','description_en','cover_image','created_at','created_by','generated_by','creator_unique_id',
                  'genres','moods','tags','songs','songs_count','likes_count','is_liked','genre_ids','mood_ids','tag_ids','song_ids']
        read_only_fields = ['id','created_at','likes_count','is_liked','generated_by','creator_unique_id']

    def get_creator_unique_id(self, obj):
        value = getattr(obj, '_creator_unique_id', None)
        if value is not None:
            return value
        if not hasattr(self, '_creator_uid'):
            self._creator_uid = User.objects.filter(
                first_name='SedaBox |', last_name='صداباکس'
            ).values_list('unique_id', flat=True).first()
        return self._creator_uid
    def get_likes_count(self, obj): return int(_metric(obj,'_likes_count',lambda: PlaylistLike.objects.filter(playlist=obj).count()))
    def get_songs(self, obj):
        return SongSummarySerializer(_ordered_playlist_songs(obj), many=True, context=self.context).data
    def get_songs_count(self, obj):
        # Match the exact visible/serialized song set. In read endpoints the songs
        # relation is prefetched with the published-song card queryset, so this
        # never reports deleted/unpublished rows that the client cannot display.
        return len(_ordered_playlist_songs(obj))
    def get_is_liked(self, obj):
        request=self.context.get('request')
        return bool(_metric(obj,'_is_liked',lambda: request and request.user.is_authenticated and PlaylistLike.objects.filter(user=request.user,playlist=obj).exists()))
    def to_representation(self, instance):
        data=super().to_representation(instance); data['cover_image']=_signed_url(data.get('cover_image')); return data


class PlaylistForEventSerializer(LocalizedModelSerializer):
    """Lightweight playlist serializer for EventPlaylist endpoint: use slim genre/mood/tag representation without slug."""
    genres = SlimGenreSerializer(many=True, read_only=True)
    moods = SlimMoodSerializer(many=True, read_only=True)
    tags = SlimTagSerializer(many=True, read_only=True)
    songs = serializers.SerializerMethodField()

    class Meta:
        model = Playlist
        list_serializer_class = OrderedPlaylistListSerializer
        fields = ['id', 'unique_id', 'title', 'title_en', 'description', 'description_en', 'cover_image', 'created_at', 'created_by', 'generated_by', 'creator_unique_id', 'genres', 'moods', 'tags', 'songs']
        read_only_fields = ['id', 'created_at']

    generated_by = serializers.CharField(source='created_by', read_only=True)
    creator_unique_id = serializers.SerializerMethodField()

    def get_creator_unique_id(self, obj):
        if not hasattr(self, '_creator_uid'):
            self._creator_uid = User.objects.filter(
                first_name="SedaBox |", last_name="صداباکس"
            ).values_list('unique_id', flat=True).first()
        return self._creator_uid

    def get_songs(self, obj):
        return SongSerializer(_ordered_playlist_songs(obj), many=True, context=self.context).data


class SongStreamSerializer(LocalizedModelSerializer):
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(read_only=True)
    artist_unique_id = serializers.CharField(source='artist.unique_id', read_only=True)
    featured_artists = serializers.SerializerMethodField()
    album_title = serializers.CharField(source='album.title', read_only=True, allow_null=True)
    uploader_unique_id = serializers.CharField(source='uploader.unique_id', read_only=True, allow_null=True)
    duration_display = serializers.ReadOnlyField()
    display_title = serializers.ReadOnlyField()
    stream_url = serializers.SerializerMethodField()
    preview_url = serializers.SerializerMethodField()
    is_preview = serializers.SerializerMethodField()
    preview_duration_seconds = serializers.SerializerMethodField()
    plays = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    genres = serializers.SerializerMethodField()
    genre_names = serializers.SerializerMethodField()
    genre_ids = serializers.SerializerMethodField()
    is_promoted = serializers.SerializerMethodField()

    class Meta:
        model = Song
        list_serializer_class = SongMetricsListSerializer
        fields = ['id','title','artist_id','artist_name','artist_unique_id','featured_artists','album','album_title',
                  'is_single','stream_url','preview_url','is_preview','preview_duration_seconds','cover_image','duration_seconds',
                  'duration_display','plays','likes_count','is_liked','genres','genre_names','genre_ids','is_promoted',
                  'status','release_date','language','description','created_at','display_title','uploader_unique_id']
        read_only_fields = fields

    def get_featured_artists(self, obj):
        request = self.context.get('request')
        return [
            {
                'id': a.id,
                'unique_id': a.unique_id,
                'url_slug': _related_url_slug(a),
                'name': localized_value(a, 'name', request),
                'name_fa': a.name,
                'name_en': a.name_en or a.name,
                'artistic_name': localized_value(a, 'artistic_name', request),
                'artistic_name_fa': a.artistic_name,
                'artistic_name_en': a.artistic_name_en or a.artistic_name,
            }
            for a in _relation_items(obj, 'featured_artists')
        ]
    def get_genres(self, obj):
        request = self.context.get('request')
        return [
            {'id': genre.id, 'name': localized_value(genre, 'name', request), 'url_slug': _related_url_slug(genre)}
            for genre in _relation_items(obj, 'genres')
        ]
    def get_genre_names(self, obj): return [item['name'] for item in self.get_genres(obj)]
    def get_genre_ids(self, obj): return [genre.id for genre in _relation_items(obj, 'genres')]
    def get_is_promoted(self, obj): return bool(getattr(obj, '_is_admin_promoted', False))
    def get_likes_count(self,obj):
        _ensure_song_metrics(obj, self.context.get('request')); return int(getattr(obj,'_likes_count',0) or 0)
    def get_is_liked(self,obj):
        request=self.context.get('request')
        _ensure_song_metrics(obj, request); return bool(getattr(obj,'_is_liked',False))
    def get_stream_url(self,obj): return _stream_wrapper(obj,self.context.get('request'))
    def get_preview_url(self,obj): return _preview_url(obj)
    def get_is_preview(self,obj):
        request=self.context.get('request'); return bool((not request or not request.user.is_authenticated) and obj.preview_audio_url)
    def get_preview_duration_seconds(self,obj): return min(30,obj.duration_seconds or 30) if obj.preview_audio_url else 0
    def get_plays(self,obj):
        _ensure_song_metrics(obj, self.context.get('request')); return int(obj.plays or 0)+int(getattr(obj,'_play_count',0) or 0)
    def to_representation(self,instance):
        data=super().to_representation(instance); data['cover_image']=_signed_url(data.get('cover_image')); return data


def normalize_user_playlist_order(value):
    """Return a stable, duplicate-free list of integer song IDs.

    Older clients stored entries as ``{"id": ..., "cover": ...}`` while the
    model contract and newer clients use plain IDs.  Accept both shapes so old
    playlists remain editable, but always persist/emit the canonical ID list.
    """
    if not isinstance(value, (list, tuple)):
        return []

    normalized = []
    seen = set()
    for item in value:
        raw_id = item.get('id') if isinstance(item, dict) else item
        try:
            song_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if song_id <= 0 or song_id in seen:
            continue
        seen.add(song_id)
        normalized.append(song_id)
    return normalized


class UserPlaylistSerializer(LocalizedModelSerializer):
    user_phone = serializers.CharField(source='user.phone_number', read_only=True)
    user_unique_id = serializers.CharField(source='user.unique_id', read_only=True)
    songs_count = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    top_three_song_covers = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='user-playlist')
    song_ids = serializers.PrimaryKeyRelatedField(queryset=Song.objects.all(), many=True, source='songs', required=False)
    order = serializers.JSONField(required=False)
    songs = serializers.SerializerMethodField()
    generated_by = serializers.ReadOnlyField(default='audience')
    creator_unique_id = serializers.CharField(source='user.unique_id', read_only=True)
    creator_user_id = serializers.IntegerField(source='user_id', read_only=True)
    creator_name = serializers.SerializerMethodField()
    is_owner = serializers.SerializerMethodField()

    class Meta:
        model = UserPlaylist
        fields = [
            'id', 'unique_id', 'user', 'user_phone', 'user_unique_id', 'title', 'public', 'songs_count',
            'likes_count', 'is_liked', 'song_ids', 'songs', 'top_three_song_covers', 'order',
            'type', 'generated_by', 'creator_unique_id', 'creator_user_id', 'creator_name', 'is_owner', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'unique_id', 'user', 'user_phone', 'user_unique_id', 'songs_count', 'likes_count',
            'is_liked', 'created_at', 'updated_at', 'top_three_song_covers', 'type',
            'songs', 'generated_by', 'creator_unique_id', 'creator_user_id', 'creator_name', 'is_owner',
        ]

    def get_creator_name(self, obj):
        name = f"{obj.user.first_name or ''} {obj.user.last_name or ''}".strip()
        return name or obj.user.unique_id or str(obj.user_id)

    def get_is_owner(self, obj):
        request = self.context.get('request')
        return bool(
            request
            and getattr(request, 'user', None)
            and request.user.is_authenticated
            and request.user.id == obj.user_id
        )

    def validate_order(self, value):
        normalized = normalize_user_playlist_order(value)
        if self.instance is not None and normalized:
            allowed_ids = set(
                self.instance.songs.filter(id__in=normalized).values_list('id', flat=True)
            )
            unknown_ids = [song_id for song_id in normalized if song_id not in allowed_ids]
            if unknown_ids:
                raise serializers.ValidationError(
                    'Order can only contain songs that belong to this playlist.'
                )
        return normalized

    def validate_title(self, value):
        title = str(value or '').strip()
        if not title:
            raise serializers.ValidationError('Playlist title cannot be empty.')
        return title

    def _songs(self, obj):
        songs = getattr(obj, '_detail_songs', None)
        if songs is None:
            songs = getattr(obj, '_card_songs', None)
        return list(songs if songs is not None else obj.songs.all())

    def get_songs_count(self, obj):
        return int(_metric(obj, '_songs_count', lambda: obj.songs.count()))

    def get_likes_count(self, obj):
        return int(_metric(obj, '_likes_count', lambda: obj.liked_by.count()))

    def get_is_liked(self, obj):
        request = self.context.get('request')
        return bool(_metric(obj, '_is_liked', lambda: request and request.user.is_authenticated and obj.liked_by.filter(id=request.user.id).exists()))

    def get_songs(self, obj):
        songs = self._songs(obj)
        song_map = {song.id: song for song in songs}
        order = normalize_user_playlist_order(obj.order)
        ordered = [song_map[song_id] for song_id in order if song_id in song_map]
        included_ids = {song.id for song in ordered}
        ordered.extend(song for song in songs if song.id not in included_ids)
        return SongSummarySerializer(ordered, many=True, context=self.context).data

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data['order'] = normalize_user_playlist_order(instance.order)
        return data

    def get_top_three_song_covers(self, obj):
        covers = []
        for song in self._songs(obj)[:3]:
            value = song.cover_image or getattr(song.album, 'cover_image', None)
            if value:
                covers.append(_signed_url(value))
        return covers



class UserPlaylistCreateSerializer(LocalizedModelSerializer):
    """Serializer for creating UserPlaylist with optional first song"""
    first_song_id = serializers.IntegerField(required=False, allow_null=True, write_only=True)
    order = serializers.JSONField(required=False)
    
    class Meta:
        model = __import__('api.models', fromlist=['UserPlaylist']).UserPlaylist
        fields = ['title', 'public', 'first_song_id', 'order']

    def validate_order(self, value):
        return normalize_user_playlist_order(value)

    def validate_title(self, value):
        title = str(value or '').strip()
        if not title:
            raise serializers.ValidationError('Playlist title cannot be empty.')
        return title
    
    def create(self, validated_data):
        first_song_id = validated_data.pop('first_song_id', None)
        request = self.context.get('request')
        
        # Create the playlist
        playlist = __import__('api.models', fromlist=['UserPlaylist']).UserPlaylist.objects.create(
            user=request.user,
            **validated_data
        )
        
        # Add the first song if provided
        if first_song_id:
            try:
                song = Song.objects.get(id=first_song_id)
                playlist.songs.add(song)
            except Song.DoesNotExist:
                pass
        
        return playlist

    def get_plays(self, obj):
        try:
            return obj.play_counts.count()
        except Exception:
            return 0


class RecommendedPlaylistListSerializer(PlaylistSummarySerializer):
    covers = serializers.SerializerMethodField()
    is_saved = serializers.SerializerMethodField()
    likes_count = serializers.SerializerMethodField()

    class Meta(PlaylistSummarySerializer.Meta):
        fields = [
            'id', 'unique_id', 'title', 'description', 'playlist_type', 'cover_image',
            'top_three_song_covers', 'covers', 'songs_count', 'is_liked', 'is_saved',
            'likes_count', 'views', 'relevance_score', 'match_percentage', 'created_at',
            'genres', 'genre_names', 'mood_names', 'type', 'generated_by', 'creator_unique_id',
        ]
        read_only_fields = fields

    def get_covers(self, obj): return self.get_top_three_song_covers(obj)
    def get_likes_count(self, obj): return int(_metric(obj, '_likes_count', lambda: obj.liked_by.count()))
    def get_is_saved(self, obj):
        request = self.context.get('request')
        return bool(_metric(obj, '_is_saved', lambda: request and request.user.is_authenticated and obj.saved_by.filter(id=request.user.id).exists()))



class RecommendedPlaylistDetailSerializer(RecommendedPlaylistListSerializer):
    songs = serializers.SerializerMethodField()
    playlist_ref = SimplePlaylistSerializer(read_only=True)

    class Meta(RecommendedPlaylistListSerializer.Meta):
        fields = RecommendedPlaylistListSerializer.Meta.fields + ['songs', 'updated_at', 'playlist_ref']
        read_only_fields = fields

    def get_songs(self, obj):
        songs = self._songs(obj)
        song_map = {song.id: song for song in songs}
        ordered = [song_map[sid] for sid in (obj.song_order or []) if sid in song_map]
        ordered.extend(song for song in songs if song not in ordered)
        return SongStreamSerializer(ordered, many=True, context=self.context).data



class SearchResultSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    type = serializers.SerializerMethodField()
    title = serializers.SerializerMethodField()
    subtitle = serializers.SerializerMethodField()
    image = serializers.SerializerMethodField()
    is_following = serializers.SerializerMethodField()
    is_liked = serializers.SerializerMethodField()
    data = serializers.SerializerMethodField()

    def get_type(self,obj):
        if isinstance(obj,Song): return 'song'
        if isinstance(obj,Artist): return 'artist'
        if isinstance(obj,Album): return 'album'
        if isinstance(obj,(Playlist,UserPlaylist)): return 'playlist'
        if isinstance(obj,User): return 'user'
        return 'unknown'
    def get_title(self, obj):
        if isinstance(obj, User):
            return obj.unique_id or f'{obj.first_name} {obj.last_name}'.strip()
        request = self.context.get('request')
        if hasattr(obj, 'title_en'):
            return localized_value(obj, 'title', request)
        if hasattr(obj, 'name_en'):
            return localized_value(obj, 'name', request)
        return getattr(obj, 'title', getattr(obj, 'name', ''))

    def get_subtitle(self, obj):
        request = self.context.get('request')
        language = get_request_language(request)
        if isinstance(obj, (Song, Album)):
            return localized_value(obj.artist, 'name', request) if obj.artist else ''
        if isinstance(obj, Artist):
            return 'Artist' if language == 'en' else 'هنرمند'
        if isinstance(obj, User):
            return 'User' if language == 'en' else 'کاربر'
        if isinstance(obj, UserPlaylist):
            owner = f'{obj.user.first_name} {obj.user.last_name}'.strip()
            if not owner:
                owner = 'SedaBox user' if language == 'en' else 'کاربر صداباکس'
            return f'By {owner}' if language == 'en' else f'از {owner}'
        if isinstance(obj, Playlist):
            creator_labels = {
                'system': ('سیستم', 'System'),
                'admin': ('صداباکس', 'SedaBox'),
                'audience': ('کاربر', 'User'),
            }
            fa_label, en_label = creator_labels.get(obj.created_by, (obj.created_by, obj.created_by))
            return en_label if language == 'en' else fa_label
        return ''
    def get_image(self,obj):
        if isinstance(obj,User):
            return user_profile_image_url(obj, self.context.get('request'))
        return _signed_url(getattr(obj,'cover_image',None) or getattr(obj,'profile_image',None)) or ''
    def get_is_following(self,obj):
        if not isinstance(obj,(Artist,User)): return None
        return bool(getattr(obj,'_is_following',False))
    def get_is_liked(self,obj): return bool(getattr(obj,'_is_liked',False))
    def get_data(self,obj):
        request=self.context.get('request')
        if isinstance(obj,Song):
            return {'duration_seconds':obj.duration_seconds,'plays':int(obj.plays or 0)+int(getattr(obj,'_play_count',0)),
                    'url_slug':_related_url_slug(obj),
                    'language':obj.language,'artist_id':obj.artist_id,'artist_name':localized_value(obj.artist, 'name', request) if obj.artist else None,
                    'artist_url_slug':_related_url_slug(obj.artist),
                    'album_id':obj.album_id,'album_name':localized_value(obj.album, 'title', request) if obj.album else None,
                    'album_url_slug':_related_url_slug(obj.album),
                    'stream_url':_stream_wrapper(obj,request),'preview_url':_preview_url(obj),
                    'is_preview':bool((not request or not request.user.is_authenticated) and obj.preview_audio_url),
                    'preview_duration_seconds':min(30,obj.duration_seconds or 30) if obj.preview_audio_url else 0}
        if isinstance(obj,Artist):
            bio = localized_value(obj, 'bio', request)
            return {'unique_id':obj.unique_id,'url_slug':_related_url_slug(obj),'verified':obj.verified,'bio':bio[:100] if bio else ''}
        if isinstance(obj,Album):
            return {'release_date':obj.release_date,'url_slug':_related_url_slug(obj),'artist_id':obj.artist_id,
                    'artist_name':localized_value(obj.artist, 'name', request) if obj.artist else None,
                    'artist_url_slug':_related_url_slug(obj.artist)}
        if isinstance(obj,(Playlist,UserPlaylist)):
            return {'url_slug':_related_url_slug(obj)}
        if isinstance(obj,User):
            is_official = (obj.unique_id or '').strip().casefold() == 'sedabox' or (
                (obj.first_name or '').strip().casefold().startswith('sedabox')
                and 'صداباکس' in (obj.last_name or '').replace(' ', '').replace('\u200c', '')
            )
            return {
                'unique_id': obj.unique_id,
                'first_name': obj.first_name,
                'last_name': obj.last_name,
                'plan': obj.plan,
                'is_verified': obj.is_verified,
                'is_official': is_official,
            }
        return {}


def _prepare_event_playlist_order(events):
    items = [item for item in events if getattr(item, 'pk', None)]
    if not items:
        return items
    through = EventPlaylist.playlists.through
    order_map = {item.pk: [] for item in items}
    for event_id, playlist_id in (
        through.objects.filter(eventplaylist_id__in=order_map)
        .order_by('eventplaylist_id', 'pk')
        .values_list('eventplaylist_id', 'playlist_id')
    ):
        order_map[int(event_id)].append(int(playlist_id))
    for item in items:
        item._ordered_playlist_ids = order_map.get(item.pk, [])
    return items


class OrderedEventPlaylistListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        iterable = data.all() if isinstance(data, BaseManager) else data
        items = list(iterable)
        _prepare_event_playlist_order(items)
        return super().to_representation(items)


def _ordered_event_playlists(event):
    """Return event playlists in the explicit order persisted by the M2M rows."""
    ordered_ids = getattr(event, '_ordered_playlist_ids', None)
    if ordered_ids is None:
        through = EventPlaylist.playlists.through
        ordered_ids = list(
            through.objects.filter(eventplaylist_id=event.pk)
            .order_by('pk')
            .values_list('playlist_id', flat=True)
        )
    playlist_map = {playlist.id: playlist for playlist in event.playlists.all()}
    return [playlist_map[playlist_id] for playlist_id in ordered_ids if playlist_id in playlist_map]


class EventPlaylistSerializer(LocalizedModelSerializer):
    """Serializer for EventPlaylist model"""
    # use compact playlist serializer that omits slug fields on nested genres/moods/tags
    playlists = serializers.SerializerMethodField()

    def get_playlists(self, obj):
        return PlaylistForEventSerializer(_ordered_event_playlists(obj), many=True, context=self.context).data
    generated_by = serializers.ReadOnlyField(default='admin')
    creator_unique_id = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='event-playlist')

    class Meta:
        model = EventPlaylist
        list_serializer_class = OrderedEventPlaylistListSerializer
        fields = ['id', 'title', 'title_en', 'time_of_day', 'playlists', 'created_at', 'updated_at', 'generated_by', 'creator_unique_id', 'type']

    def get_creator_unique_id(self, obj):
        return _official_creator_uid(self)


class PlaylistCoverSerializer(LocalizedModelSerializer):
    """Lightweight playlist serializer used in EventPlaylist list views.
    Uses the first song's cover image as the playlist cover when available.
    """
    cover_image = serializers.SerializerMethodField()
    top_song_covers = serializers.SerializerMethodField()
    songs_count = serializers.SerializerMethodField()
    generated_by = serializers.CharField(source='created_by', read_only=True)
    creator_unique_id = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='normal-playlist')

    class Meta:
        model = Playlist
        fields = ['id', 'title', 'description', 'cover_image', 'top_song_covers', 'songs_count', 'generated_by', 'creator_unique_id', 'type']
        read_only_fields = fields

    def get_creator_unique_id(self, obj):
        return _official_creator_uid(self)

    def _songs(self, obj):
        prefetched = getattr(obj, '_prefetched_objects_cache', {}).get('songs')
        return list(prefetched) if prefetched is not None else list(obj.songs.all())

    def get_songs_count(self, obj):
        return len(self._songs(obj))

    def get_top_song_covers(self, obj):
        covers = []
        for song in self._songs(obj):
            value = getattr(song, 'cover_image', None) or getattr(getattr(song, 'album', None), 'cover_image', None)
            if value:
                covers.append(_signed_url(value))
            if len(covers) == 2:
                break
        return covers

    def get_cover_image(self, obj):
        songs = self._songs(obj)
        first_song = songs[0] if songs else None
        if first_song:
            value = getattr(first_song, 'cover_image', None) or getattr(
                getattr(first_song, 'album', None), 'cover_image', None
            )
            if value:
                return _signed_url(value)
        return _signed_url(getattr(obj, 'cover_image', None))


class EventPlaylistListSerializer(LocalizedModelSerializer):
    """Serializer for listing EventPlaylists with lightweight playlist covers."""
    playlists = serializers.SerializerMethodField()

    def get_playlists(self, obj):
        return PlaylistCoverSerializer(_ordered_event_playlists(obj), many=True, context=self.context).data
    generated_by = serializers.ReadOnlyField(default='admin')
    creator_unique_id = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='event-playlist')

    class Meta:
        model = EventPlaylist
        list_serializer_class = OrderedEventPlaylistListSerializer
        fields = ['id', 'title', 'title_en', 'time_of_day', 'playlists', 'created_at', 'updated_at', 'generated_by', 'creator_unique_id', 'type']
        read_only_fields = fields

    def get_creator_unique_id(self, obj):
        return _official_creator_uid(self)


class PlaylistDetailForEventSerializer(LocalizedModelSerializer):
    """Playlist serializer for EventPlaylist detail — uses SongSummarySerializer for songs."""
    genres = SlimGenreSerializer(many=True, read_only=True)
    moods = SlimMoodSerializer(many=True, read_only=True)
    tags = SlimTagSerializer(many=True, read_only=True)
    songs = serializers.SerializerMethodField()
    generated_by = serializers.CharField(source='created_by', read_only=True)
    creator_unique_id = serializers.SerializerMethodField()

    def get_creator_unique_id(self, obj):
        return _official_creator_uid(self)

    def get_songs(self, obj):
        return SongSummarySerializer(_ordered_playlist_songs(obj), many=True, context=self.context).data

    class Meta:
        model = Playlist
        list_serializer_class = OrderedPlaylistListSerializer
        fields = ['id', 'title', 'title_en', 'description', 'description_en', 'cover_image', 'created_at', 'created_by', 'generated_by', 'creator_unique_id', 'genres', 'moods', 'tags', 'songs']
        read_only_fields = fields


class EventPlaylistDetailSerializer(LocalizedModelSerializer):
    """Detailed EventPlaylist serializer returning playlists with summarized songs."""
    playlists = serializers.SerializerMethodField()

    def get_playlists(self, obj):
        return PlaylistDetailForEventSerializer(_ordered_event_playlists(obj), many=True, context=self.context).data
    generated_by = serializers.ReadOnlyField(default='admin')
    creator_unique_id = serializers.SerializerMethodField()
    type = serializers.ReadOnlyField(default='event-playlist')

    class Meta:
        model = EventPlaylist
        fields = ['id', 'title', 'title_en', 'time_of_day', 'playlists', 'created_at', 'updated_at', 'generated_by', 'creator_unique_id', 'type']
        read_only_fields = fields

    def get_creator_unique_id(self, obj):
        return _official_creator_uid(self)


class SearchSectionSerializer(LocalizedModelSerializer):
    """Serializer for SearchSection model
    Use `SongSummarySerializer` for song-type sections to keep responses lightweight.
    """
    songs = serializers.SerializerMethodField()
    albums = AlbumSerializer(many=True, read_only=True)
    playlists = PlaylistSerializer(many=True, read_only=True)
    
    song_ids = serializers.PrimaryKeyRelatedField(
        queryset=Song.objects.all(), many=True, write_only=True, source='songs', required=False
    )
    album_ids = serializers.PrimaryKeyRelatedField(
        queryset=Album.objects.all(), many=True, write_only=True, source='albums', required=False
    )
    playlist_ids = serializers.PrimaryKeyRelatedField(
        queryset=Playlist.objects.all(), many=True, write_only=True, source='playlists', required=False
    )

    created_by_name = serializers.ReadOnlyField(source='created_by.phone_number')
    updated_by_name = serializers.ReadOnlyField(source='updated_by.phone_number')
    created_by_unique_id = serializers.ReadOnlyField(source='created_by.unique_id')
    updated_by_unique_id = serializers.ReadOnlyField(source='updated_by.unique_id')

    class Meta:
        model = SearchSection
        fields = [
            'id', 'type', 'title', 'title_en', 'icon_logo', 'item_size', 
            'songs', 'albums', 'playlists', 
            'song_ids', 'album_ids', 'playlist_ids',
            'created_at', 'updated_at', 'created_by', 'updated_by',
            'created_by_name', 'updated_by_name', 'created_by_unique_id', 'updated_by_unique_id'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'created_by', 'updated_by']

    def get_songs(self, obj):
        try:
            t = (obj.type or '').lower()
        except Exception:
            t = ''

        # If this section represents songs, use the lightweight SongSummarySerializer
        if 'song' in t:
            return SongSummarySerializer(obj.songs.all(), many=True, context=self.context).data

        # otherwise return full SongSerializer (fallback)
        return SongSerializer(obj.songs.all(), many=True, context=self.context).data


class SessionSerializer(LocalizedModelSerializer):
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = RefreshToken
        fields = ['id', 'ip', 'user_agent', 'device_name', 'device_type', 'os_info', 'created_at', 'is_current']

    def get_is_current(self, obj):
        current_token = self.context.get('current_token')
        if not current_token:
            return False
        # Lazy import avoids the serializers/auth_views import cycle and keeps
        # both legacy PBKDF2 and current HMAC refresh-token hashes compatible.
        from .auth_views import check_refresh_token
        return check_refresh_token(current_token, obj.token_hash)


class LikedSongSerializer(LocalizedModelSerializer):
    when_liked = serializers.SerializerMethodField()
    
    class Meta:
        model = SongLike
        fields = ['id', 'song', 'when_liked']
    
    def get_when_liked(self, obj):
        from django.utils import timezone
        delta = timezone.now() - obj.created_at
        days = delta.days
        return _relative_day_label(days, self.context.get('request'))

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Nest the song data
        song_data = SongStreamSerializer(instance.song, context=self.context).data
        ret.update(song_data)
        # Remove the 'song' ID field to avoid confusion
        ret.pop('song', None)
        return ret


class LikedAlbumSerializer(LocalizedModelSerializer):
    when_liked = serializers.SerializerMethodField()
    
    class Meta:
        model = AlbumLike
        fields = ['id', 'album', 'when_liked']
    
    def get_when_liked(self, obj):
        from django.utils import timezone
        delta = timezone.now() - obj.created_at
        days = delta.days
        return _relative_day_label(days, self.context.get('request'))

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Nest the album data
        album_data = AlbumSerializer(instance.album, context=self.context).data
        ret.update(album_data)
        # Remove the 'album' ID field
        ret.pop('album', None)
        return ret


class LikedPlaylistSerializer(LocalizedModelSerializer):
    when_liked = serializers.SerializerMethodField()
    
    class Meta:
        model = PlaylistLike
        fields = ['id', 'playlist', 'when_liked']
    
    def get_when_liked(self, obj):
        from django.utils import timezone
        delta = timezone.now() - obj.created_at
        days = delta.days
        return _relative_day_label(days, self.context.get('request'))

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Nest the playlist data
        playlist_data = PlaylistSerializer(instance.playlist, context=self.context).data
        ret.update(playlist_data)
        # Remove the 'playlist' ID field
        ret.pop('playlist', None)
        return ret


class RulesSerializer(LocalizedModelSerializer):
    class Meta:
        model = Rules
        fields = ['id', 'title', 'title_en', 'content', 'content_en', 'version', 'created_at']
        read_only_fields = ['version', 'created_at']

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Ensure `updated_at` is never returned in API responses
        ret.pop('updated_at', None)
        return ret


class DepositRequestSerializer(LocalizedModelSerializer):
    artist_name = serializers.CharField(source='artist.name', read_only=True)
    artist_id = serializers.IntegerField(source='artist.id', read_only=True)
    artist_unique_id = serializers.CharField(source='artist.unique_id', read_only=True)

    class Meta:
        model = DepositRequest
        fields = [
            'id', 'artist_id', 'artist_name', 'artist_unique_id', 'amount', 'status', 
            'transaction_id', 'submission_date', 'status_change_date', 'summary'
        ]
        read_only_fields = ['id', 'artist_id', 'status', 'submission_date', 'status_change_date', 'summary']


class SupportTicketSerializer(LocalizedModelSerializer):
    """Artist-facing support ticket serializer. Admin-only fields are read-only."""

    class Meta:
        model = SupportTicket
        fields = [
            'id', 'subject', 'message', 'status', 'admin_response',
            'responded_at', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'status', 'admin_response', 'responded_at', 'created_at', 'updated_at',
        ]

    def validate_subject(self, value):
        value = str(value or '').strip()
        if len(value) < 3:
            raise serializers.ValidationError('موضوع تیکت باید حداقل ۳ نویسه باشد.')
        return value

    def validate_message(self, value):
        value = str(value or '').strip()
        if len(value) < 5:
            raise serializers.ValidationError('متن تیکت باید حداقل ۵ نویسه باشد.')
        return value


class ReportSerializer(LocalizedModelSerializer):
    artist_id = serializers.IntegerField(source='artist.id', required=False, allow_null=True)
    artist_unique_id = serializers.CharField(source='artist.unique_id', read_only=True)
    reported_user_phone = serializers.CharField(source='reported_user.phone_number', read_only=True)

    class Meta:
        model = Report
        fields = ['id', 'song', 'artist', 'artist_id', 'artist_unique_id', 'reported_user', 'reported_user_phone', 'text', 'created_at']
        read_only_fields = ['id', 'created_at']

    def create(self, validated_data):
        # ``artist_id`` intentionally keeps the existing public request/response
        # shape, but DRF materializes ``source='artist.id'`` as a nested dict.
        # Resolve that dict to the actual relation before ModelSerializer.create
        # so report submission remains behavior-compatible and no nested write is
        # attempted.
        artist_value = validated_data.get('artist')
        if isinstance(artist_value, dict):
            artist_id = artist_value.get('id')
            if artist_id is None:
                validated_data.pop('artist', None)
            else:
                try:
                    validated_data['artist'] = Artist.objects.get(pk=artist_id)
                except Artist.DoesNotExist:
                    raise serializers.ValidationError({'artist_id': ['هنرمند انتخاب‌شده پیدا نشد.']})
        return super().create(validated_data)

    def validate(self, data):
        # Require exactly one target: song, artist, or reported_user
        has_song = bool(data.get('song'))
        has_artist = bool(data.get('artist'))
        has_reported_user = bool(data.get('reported_user'))
        total = sum([has_song, has_artist, has_reported_user])
        if total == 0:
            raise serializers.ValidationError("One of song, artist or reported_user must be provided.")
        if total > 1:
            raise serializers.ValidationError("Provide only one of song, artist or reported_user.")
        return data


class NotificationSerializer(LocalizedModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'recipient_role', 'text', 'text_en', 'has_read', 'created_at']
        read_only_fields = ['id', 'created_at']


class AudioAdSerializer(LocalizedModelSerializer):
    """Public serializer for audio ad objects."""
    audio_url = serializers.SerializerMethodField()
    image_cover = serializers.SerializerMethodField()

    class Meta:
        model = AudioAd
        fields = [
            'id', 'title', 'title_en', 'audio_url', 'image_cover', 'navigate_link',
            'duration', 'skippable_after', 'is_active', 'created_at'
        ]
        read_only_fields = fields

    def get_audio_url(self, obj):
        if not obj.audio_url:
            return None
        signed = generate_signed_r2_url(obj.audio_url)
        return signed if signed else obj.audio_url

    def get_image_cover(self, obj):
        if not obj.image_cover:
            return None
        signed = generate_signed_r2_url(obj.image_cover)
        return signed if signed else obj.image_cover


class DownloadHistorySerializer(LocalizedModelSerializer):
    """Serializer for user download history entries."""
    song = SongSummarySerializer(read_only=True)
    last_download_quality = serializers.SerializerMethodField()

    class Meta:
        model = DownloadHistory
        fields = ['id', 'song', 'updated_at', 'last_download_quality']
        read_only_fields = ['id', 'song', 'updated_at', 'last_download_quality']

    def get_last_download_quality(self, obj):
        quality_map = self.context.get('download_quality_map') or {}
        value = quality_map.get(obj.song_id)
        return str(value) if value in {'128', '320'} else None


class InitialCheckSerializer(LocalizedModelSerializer):
    """Serializer for initial genre selection for personalization"""
    genres = GenreSerializer(many=True, read_only=True)
    genre_ids = serializers.PrimaryKeyRelatedField(
        queryset=Genre.objects.all(), many=True, write_only=True, source='genres'
    )

    class Meta:
        model = InitialCheck
        fields = ['id', 'genres', 'genre_ids', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

    def create(self, validated_data):
        user = self.context['request'].user
        genres = validated_data.pop('genres', [])
        initial_check, created = InitialCheck.objects.get_or_create(user=user)
        initial_check.genres.set(genres)
        return initial_check


