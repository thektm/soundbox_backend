"""Failure-safe helpers for read-heavy API endpoints."""

import hashlib
import json
import time
from datetime import timedelta

from django.core.cache import cache
from django.db import connection
from django.db.models import Count
from django.utils import timezone

from .models import ActivePlayback, AlbumLike, Artist, ArtistMonthlyListener, Follow, PlaylistLike, Song, SongLike, User, UserPlaylist
from .song_play_metrics import hydrate_song_play_counts

CATALOG_VERSION_KEY = "catalog-version"
AFFINITY_VERSION_KEY = "affinity-version"
USER_DIRECTORY_VERSION_KEY = "user-directory-version"
USER_AFFINITY_VERSION_PREFIX = "user-affinity-version"


def stable_cache_key(prefix, *parts):
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:24]
    return f"{prefix}:{digest}"


def cache_get(key):
    try:
        return cache.get(key)
    except Exception:
        return None


def cache_set(key, value, timeout):
    try:
        cache.set(key, value, timeout=timeout)
        cache.delete(f"{key}:building")
    except Exception:
        pass


def cache_delete(*keys):
    try:
        cache.delete_many([key for key in keys if key])
    except Exception:
        pass


def cache_increment(key, timeout):
    try:
        if cache.add(key, 0, timeout=timeout):
            return cache.incr(key)
        return cache.incr(key)
    except Exception:
        return 1


def cache_version(key):
    value = cache_get(key)
    return value if isinstance(value, int) else 0




def user_affinity_version(user_id):
    if not user_id:
        return 0
    return cache_version(f"{USER_AFFINITY_VERSION_PREFIX}:{int(user_id)}")


def bump_user_affinity_version(user_id, timeout=7 * 24 * 60 * 60):
    if not user_id:
        return 0
    return cache_increment(f"{USER_AFFINITY_VERSION_PREFIX}:{int(user_id)}", timeout)

def cache_get_or_claim(key, lock_timeout=20, wait_timeout=1.2):
    cached = cache_get(key)
    if cached is not None:
        return cached, False
    lock_key = f"{key}:building"
    try:
        if cache.add(lock_key, 1, timeout=lock_timeout):
            return None, True
    except Exception:
        return None, True
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        time.sleep(0.05)
        cached = cache_get(key)
        if cached is not None:
            return cached, False
    return None, True


def relation_ids(obj, name):
    prefetched = getattr(obj, "_prefetched_objects_cache", {}).get(name)
    if prefetched is not None:
        return {item.pk for item in prefetched}
    return set(getattr(obj, name).values_list("id", flat=True))


def hydrate_song_metrics(songs, user=None, include_playlist_count=True):
    items = list(songs)
    ids = [item.pk for item in items if item.pk]
    if not ids:
        return items
    required = ('_play_count', '_likes_count', '_playlist_count', '_playlist_users_count', '_is_liked')
    if all(all(hasattr(item, attr) for attr in required) for item in items):
        return items
    hydrate_song_play_counts(items)
    likes = {
        row["song_id"]: row["total"]
        for row in SongLike.objects.filter(song_id__in=ids)
        .values("song_id").annotate(total=Count("id"))
    }
    playlist_counts = {}
    playlist_users = {}
    if include_playlist_count:
        links = UserPlaylist.songs.through.objects.filter(song_id__in=ids)
        playlist_counts = {
            row["song_id"]: row["total"]
            for row in links.values("song_id").annotate(total=Count("userplaylist_id"))
        }
        playlist_users = {
            row["song_id"]: row["total"]
            for row in links.values("song_id").annotate(total=Count("userplaylist__user_id", distinct=True))
        }
    liked = set()
    if user is not None and getattr(user, "is_authenticated", False):
        liked = set(SongLike.objects.filter(user=user, song_id__in=ids).values_list("song_id", flat=True))
    for song in items:
        song._play_count = int(getattr(song, "_cached_tracked_plays", 0) or 0)
        song._likes_count = likes.get(song.pk, 0)
        song._playlist_count = playlist_counts.get(song.pk, 0)
        song._playlist_users_count = playlist_users.get(song.pk, 0)
        song._is_liked = song.pk in liked
    return items


def hydrate_album_metrics(albums, user=None):
    items = list(albums)
    ids = [item.pk for item in items if item.pk]
    if not ids:
        return items
    counts = {
        row["album_id"]: row["total"]
        for row in AlbumLike.objects.filter(album_id__in=ids)
        .values("album_id").annotate(total=Count("id"))
    }
    liked = set()
    if user is not None and getattr(user, "is_authenticated", False):
        liked = set(AlbumLike.objects.filter(user=user, album_id__in=ids).values_list("album_id", flat=True))
    for album in items:
        album._likes_count = counts.get(album.pk, 0)
        album._is_liked = album.pk in liked
    return items


def hydrate_artist_metrics(artists, user=None):
    items = list(artists)
    ids = [item.pk for item in items if item.pk]
    if not ids:
        return items
    followers = {
        row["followed_artist_id"]: row["total"]
        for row in Follow.objects.filter(followed_artist_id__in=ids)
        .values("followed_artist_id").annotate(total=Count("id"))
    }
    following = {
        row["follower_artist_id"]: row["total"]
        for row in Follow.objects.filter(follower_artist_id__in=ids)
        .values("follower_artist_id").annotate(total=Count("id"))
    }
    monthly = {
        row["artist_id"]: row["total"]
        for row in ArtistMonthlyListener.objects.filter(
            artist_id__in=ids,
            updated_at__gte=timezone.now() - timedelta(days=28),
        ).values("artist_id").annotate(total=Count("user_id", distinct=True))
    }
    followed = set()
    if user is not None and getattr(user, "is_authenticated", False):
        followed = set(Follow.objects.filter(
            follower_user=user, followed_artist_id__in=ids
        ).values_list("followed_artist_id", flat=True))
    for artist in items:
        artist._followers_count = followers.get(artist.pk, 0)
        artist._followings_count = following.get(artist.pk, 0)
        artist._monthly_listeners_count = monthly.get(artist.pk, 0)
        artist._is_following = artist.pk in followed
    return items


def hydrate_playlist_metrics(playlists, user=None):
    items = list(playlists)
    ids = [item.pk for item in items if item.pk]
    if not ids:
        return items
    counts = {
        row["playlist_id"]: row["total"]
        for row in PlaylistLike.objects.filter(playlist_id__in=ids)
        .values("playlist_id").annotate(total=Count("id"))
    }
    liked = set()
    if user is not None and getattr(user, "is_authenticated", False):
        liked = set(PlaylistLike.objects.filter(user=user, playlist_id__in=ids).values_list("playlist_id", flat=True))
    for playlist in items:
        playlist._likes_count = counts.get(playlist.pk, 0)
        playlist._is_liked = playlist.pk in liked
    return items


def hydrate_followable_metrics(entities, user=None):
    """Attach follow metrics for a mixed User/Artist collection in fixed queries."""
    items = list(entities)
    artists = [item for item in items if isinstance(item, Artist) and item.pk]
    users = [item for item in items if isinstance(item, User) and item.pk]
    artist_ids = [item.pk for item in artists]
    user_ids = [item.pk for item in users]

    artist_followers = {}
    artist_following = {}
    user_followers = {}
    user_following = {}
    if artist_ids:
        artist_followers = {
            row['followed_artist_id']: row['total']
            for row in Follow.objects.filter(followed_artist_id__in=artist_ids)
            .values('followed_artist_id').annotate(total=Count('id'))
        }
        artist_following = {
            row['follower_artist_id']: row['total']
            for row in Follow.objects.filter(follower_artist_id__in=artist_ids)
            .values('follower_artist_id').annotate(total=Count('id'))
        }
    if user_ids:
        user_followers = {
            row['followed_user_id']: row['total']
            for row in Follow.objects.filter(followed_user_id__in=user_ids)
            .values('followed_user_id').annotate(total=Count('id'))
        }
        user_following = {
            row['follower_user_id']: row['total']
            for row in Follow.objects.filter(follower_user_id__in=user_ids)
            .values('follower_user_id').annotate(total=Count('id'))
        }

    followed_artists = set()
    followed_users = set()
    if user is not None and getattr(user, 'is_authenticated', False):
        if artist_ids:
            followed_artists = set(Follow.objects.filter(
                follower_user=user, followed_artist_id__in=artist_ids,
            ).values_list('followed_artist_id', flat=True))
        if user_ids:
            followed_users = set(Follow.objects.filter(
                follower_user=user, followed_user_id__in=user_ids,
            ).values_list('followed_user_id', flat=True))

    for artist in artists:
        artist._followers_count = artist_followers.get(artist.pk, 0)
        artist._followings_count = artist_following.get(artist.pk, 0)
        artist._is_following = artist.pk in followed_artists
    for item in users:
        item._followers_count = user_followers.get(item.pk, 0)
        item._followings_count = user_following.get(item.pk, 0)
        item._is_following = item.pk in followed_users
    return items


def _artist_follow_page_rows(artist_ids, *, followed_side, offset, page_size):
    """Fetch one ordered follow page per artist with a single PostgreSQL query."""
    if not artist_ids or connection.vendor != 'postgresql':
        return None
    table = connection.ops.quote_name(Follow._meta.db_table)
    owner_column = 'followed_artist_id' if followed_side else 'follower_artist_id'
    user_column = 'follower_user_id' if followed_side else 'followed_user_id'
    artist_column = 'follower_artist_id' if followed_side else 'followed_artist_id'
    placeholders = ','.join(['%s'] * len(artist_ids))
    start = max(0, int(offset))
    end = start + max(0, int(page_size))
    sql = f'''\
        SELECT owner_id, related_user_id, related_artist_id
        FROM (
            SELECT {owner_column} AS owner_id,
                   {user_column} AS related_user_id,
                   {artist_column} AS related_artist_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY {owner_column}
                       ORDER BY created_at DESC, id DESC
                   ) AS rn
            FROM {table}
            WHERE {owner_column} IN ({placeholders})
        ) ranked
        WHERE rn > %s AND rn <= %s
        ORDER BY owner_id, rn
    '''
    with connection.cursor() as cursor:
        cursor.execute(sql, [*artist_ids, start, end])
        return cursor.fetchall()


def hydrate_artist_full_list(
    artists,
    user=None,
    *,
    followers_offset=0,
    followers_page_size=10,
    following_offset=0,
    following_page_size=10,
):
    """Prepare the expensive full ArtistSerializer payload in bounded queries."""
    items = list(artists)
    ids = [item.pk for item in items if item.pk]
    if not ids:
        return items

    hydrate_artist_metrics(items, user)
    live = {
        row['song__artist_id']: row['total']
        for row in ActivePlayback.objects.filter(
            song__artist_id__in=ids,
            expiration_time__gt=timezone.now(),
        ).values('song__artist_id').annotate(total=Count('user_id', distinct=True))
    }
    for artist in items:
        artist._live_listeners_count = live.get(artist.pk, 0)

    follower_rows = _artist_follow_page_rows(
        ids, followed_side=True, offset=followers_offset, page_size=followers_page_size,
    )
    following_rows = _artist_follow_page_rows(
        ids, followed_side=False, offset=following_offset, page_size=following_page_size,
    )
    if follower_rows is None or following_rows is None:
        return items

    related_user_ids = {
        row[1] for row in [*follower_rows, *following_rows] if row[1] is not None
    }
    related_artist_ids = {
        row[2] for row in [*follower_rows, *following_rows] if row[2] is not None
    }
    users_by_id = User.objects.in_bulk(related_user_ids)
    artists_by_id = Artist.objects.in_bulk(related_artist_ids)

    followers_by_owner = {artist_id: [] for artist_id in ids}
    following_by_owner = {artist_id: [] for artist_id in ids}
    nested = []
    for owner_id, user_id, artist_id in follower_rows:
        entity = users_by_id.get(user_id) if user_id is not None else artists_by_id.get(artist_id)
        if entity is not None:
            followers_by_owner.setdefault(owner_id, []).append(entity)
            nested.append(entity)
    for owner_id, user_id, artist_id in following_rows:
        entity = users_by_id.get(user_id) if user_id is not None else artists_by_id.get(artist_id)
        if entity is not None:
            following_by_owner.setdefault(owner_id, []).append(entity)
            nested.append(entity)

    unique_nested = list({(type(item), item.pk): item for item in nested if item.pk}.values())
    hydrate_followable_metrics(unique_nested, user)
    for artist in items:
        artist._followers_page_items = followers_by_owner.get(artist.pk, [])
        artist._following_page_items = following_by_owner.get(artist.pk, [])
    return items
