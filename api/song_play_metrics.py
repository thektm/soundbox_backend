"""Fast, coherent song play-count cache for read-heavy admin endpoints.

PostgreSQL remains the source of truth. Redis only mirrors the tracked
``Song.play_counts`` cardinality so normal admin song lists never need to join
and count the large play relation.

Coherency is guarded by the latest ``PlayCount`` primary key. Redis stores the
last play id it has observed plus a namespace. If PostgreSQL is ahead (for
example because Redis was temporarily unavailable during a committed play),
the namespace is bumped before any cached value can be returned. Old values
then become unreachable immediately and are lazily rebuilt from PostgreSQL.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.conf import settings
from django.db.models import Count

from .models import PlayCount, Song
from .recommendation_runtime import get_redis_client

logger = logging.getLogger(__name__)

_NAMESPACE_KEY = "sedabox:metrics:song-plays:namespace:v1"
_LAST_PLAY_ID_KEY = "sedabox:metrics:song-plays:last-play-id:v1"
_COUNT_PREFIX = "sedabox:metrics:song-plays:tracked:v1"
_DEFAULT_TTL = 6 * 60 * 60

_ENSURE_NAMESPACE_LUA = r"""
local ns = tonumber(redis.call('GET', KEYS[1]) or '1')
local last_id = tonumber(redis.call('GET', KEYS[2]) or '0')
local db_last_id = tonumber(ARGV[1]) or 0
if redis.call('EXISTS', KEYS[1]) == 0 then
    redis.call('SET', KEYS[1], ns)
end
if db_last_id > last_id then
    ns = redis.call('INCR', KEYS[1])
    redis.call('SET', KEYS[2], db_last_id)
end
return ns
"""

_RECORD_PLAY_LUA = r"""
local ns = tonumber(redis.call('GET', KEYS[1]) or '1')
local last_id = tonumber(redis.call('GET', KEYS[2]) or '0')
local play_id = tonumber(ARGV[1]) or 0
local song_id = ARGV[2]
local ttl = tonumber(ARGV[3])
if redis.call('EXISTS', KEYS[1]) == 0 then
    redis.call('SET', KEYS[1], ns)
end

-- A gap means Redis may have missed one or more committed plays. Rotate the
-- namespace instead of risking a stale cached count.
if play_id > (last_id + 1) then
    ns = redis.call('INCR', KEYS[1])
end

local count_key = ARGV[4] .. ':' .. ns .. ':' .. song_id
if redis.call('EXISTS', count_key) == 1 then
    redis.call('INCRBY', count_key, 1)
    redis.call('EXPIRE', count_key, ttl)
end
if play_id > last_id then
    redis.call('SET', KEYS[2], play_id)
end
return ns
"""

_ROTATE_NAMESPACE_LUA = r"""
local ns = redis.call('INCR', KEYS[1])
redis.call('SET', KEYS[2], ARGV[1])
return ns
"""

_SET_IF_COHERENT_LUA = r"""
local current_ns = tonumber(redis.call('GET', KEYS[1]) or '1')
local current_last_id = tonumber(redis.call('GET', KEYS[2]) or '0')
local expected_ns = tonumber(ARGV[1])
local expected_last_id = tonumber(ARGV[2])
if current_ns ~= expected_ns or current_last_id ~= expected_last_id then
    return 0
end
local ttl = tonumber(ARGV[3])
for i = 4, #ARGV, 2 do
    local key = ARGV[i]
    local value = ARGV[i + 1]
    redis.call('SET', key, value, 'EX', ttl)
end
return 1
"""


def _ttl() -> int:
    return max(60, int(getattr(settings, "SONG_PLAY_COUNT_CACHE_TTL", _DEFAULT_TTL)))


def _latest_play_id() -> int:
    value = PlayCount.objects.order_by("-pk").values_list("pk", flat=True).first()
    return int(value or 0)


def _db_tracked_counts(song_ids: Iterable[int]) -> dict[int, int]:
    ids = [int(song_id) for song_id in song_ids if song_id]
    if not ids:
        return {}
    through = Song.play_counts.through
    rows = (
        through.objects.filter(song_id__in=ids)
        .values("song_id")
        .annotate(total=Count("playcount_id"))
    )
    result = {song_id: 0 for song_id in ids}
    result.update({int(row["song_id"]): int(row["total"] or 0) for row in rows})
    return result


def _coherent_namespace(client) -> tuple[int, int] | None:
    """Return ``(namespace, latest_db_play_id)`` when Redis is coherent."""
    db_last_id = _latest_play_id()
    try:
        namespace = int(client.eval(
            _ENSURE_NAMESPACE_LUA,
            2,
            _NAMESPACE_KEY,
            _LAST_PLAY_ID_KEY,
            db_last_id,
        ))
        return namespace, db_last_id
    except Exception as exc:
        logger.warning("Song play-count Redis coherence check failed: %s", exc)
        return None


def _count_key(namespace: int, song_id: int) -> str:
    return f"{_COUNT_PREFIX}:{namespace}:{int(song_id)}"


def get_tracked_song_play_counts(song_ids: Iterable[int]) -> dict[int, int]:
    """Return exact tracked-play counts for IDs with one Redis bulk read.

    PostgreSQL is always authoritative. Redis is accepted only after the same
    latest-PlayCount coherence check used by serializers, so callers such as
    trending can reuse all-time counts without scanning the full relation.
    """
    ids = list(dict.fromkeys(int(song_id) for song_id in song_ids if song_id))
    if not ids:
        return {}

    counts: dict[int, int] = {}
    client = get_redis_client()
    coherence = _coherent_namespace(client) if client is not None else None
    namespace, expected_last_id = coherence if coherence is not None else (None, None)

    missing = list(ids)
    if client is not None and namespace is not None:
        try:
            values = client.mget([_count_key(namespace, song_id) for song_id in ids])
            missing = []
            for song_id, value in zip(ids, values):
                if value is None:
                    missing.append(song_id)
                    continue
                try:
                    counts[song_id] = max(0, int(value))
                except (TypeError, ValueError):
                    missing.append(song_id)
        except Exception as exc:
            logger.warning("Song play-count Redis bulk read failed: %s", exc)
            namespace = None
            missing = list(ids)

    if missing:
        exact = _db_tracked_counts(missing)
        counts.update(exact)
        if client is not None and namespace is not None and exact:
            args: list[object] = [namespace, expected_last_id, _ttl()]
            for song_id, value in exact.items():
                args.extend((_count_key(namespace, song_id), int(value)))
            try:
                # Do not populate an old snapshot if a play committed while the
                # PostgreSQL count query was running.
                client.eval(
                    _SET_IF_COHERENT_LUA, 2,
                    _NAMESPACE_KEY, _LAST_PLAY_ID_KEY, *args
                )
            except Exception as exc:
                logger.warning("Song play-count Redis warm failed: %s", exc)

    return {song_id: int(counts.get(song_id, 0)) for song_id in ids}


def hydrate_song_play_counts(songs):
    """Attach exact tracked-play counts with one Redis MGET / one DB fallback."""
    items = list(songs)
    song_ids = [int(song.pk) for song in items if getattr(song, "pk", None)]
    counts = get_tracked_song_play_counts(song_ids)
    for song in items:
        if getattr(song, "pk", None):
            song._cached_tracked_plays = int(counts.get(int(song.pk), 0))
    return items


def apply_annotated_song_play_counts(songs):
    """Attach exact DB annotations without re-querying or warming Redis.

    These annotations were computed before a Redis coherence marker was read, so
    using them to warm the cache could race a concurrent play. They are still
    perfect for the current response (notably explicit play sorting).
    """
    items = list(songs)
    for song in items:
        if getattr(song, "pk", None):
            song._cached_tracked_plays = max(
                0, int(getattr(song, "tracked_plays", 0) or 0)
            )
    return items


def record_committed_song_play(song_id: int, play_count_id: int) -> None:
    """Mirror one committed relation add atomically when a cache entry exists."""
    client = get_redis_client()
    if client is None:
        return
    try:
        client.eval(
            _RECORD_PLAY_LUA,
            2,
            _NAMESPACE_KEY,
            _LAST_PLAY_ID_KEY,
            int(play_count_id),
            int(song_id),
            _ttl(),
            _COUNT_PREFIX,
        )
    except Exception as exc:
        # PostgreSQL is already committed. Do not fail accounting; the next read
        # detects that PostgreSQL's latest PlayCount id is ahead and rotates the
        # namespace before using Redis again.
        logger.warning("Song play-count Redis write-through failed: %s", exc)


def invalidate_song_play_count_cache() -> None:
    """Rotate all count keys after uncommon remove/clear/delete mutations."""
    client = get_redis_client()
    if client is None:
        return
    try:
        client.eval(
            _ROTATE_NAMESPACE_LUA,
            2,
            _NAMESPACE_KEY,
            _LAST_PLAY_ID_KEY,
            _latest_play_id(),
        )
    except Exception as exc:
        logger.warning("Song play-count Redis invalidation failed: %s", exc)
