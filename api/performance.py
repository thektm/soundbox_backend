"""Failure-safe helpers for read-heavy API endpoints."""

import hashlib
import json
import time
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count
from django.utils import timezone

from .models import AlbumLike, ArtistMonthlyListener, Follow, PlaylistLike, Song, SongLike, UserPlaylist

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
    through = Song.play_counts.through
    plays = {
        row["song_id"]: row["total"]
        for row in through.objects.filter(song_id__in=ids)
        .values("song_id").annotate(total=Count("playcount_id"))
    }
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
        song._play_count = plays.get(song.pk, 0)
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
