"""Redis-backed recommendation freshness and generated-playlist maintenance.

The helpers in this module are deliberately failure-safe: recommendation reads
continue to work when Redis is temporarily unavailable, while destructive
cleanup is skipped unless Redis can confirm that a row has not been used.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
from datetime import timedelta
from collections.abc import Iterable, Sequence
from typing import Any, TypeVar

from django.conf import settings
from django.db import close_old_connections
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

T = TypeVar("T")

_REDIS_CLIENT = None
_REDIS_CLIENT_LOCK = threading.Lock()
_MAINTENANCE_STARTED = False
_MAINTENANCE_LOCK = threading.Lock()
_LOCAL_FRESH_COUNTERS: dict[str, int] = {}
_LOCAL_FRESH_LOCK = threading.Lock()

_USAGE_ZSET = "sedabox:generated-playlists:last-used"
_CLEANUP_LOCK = "sedabox:generated-playlists:cleanup-lock"
_STARTUP_GUARD = "sedabox:generated-playlists:startup-guard"
_PERIODIC_DUE = "sedabox:generated-playlists:periodic-due"


def get_redis_client():
    """Return one pooled Redis client per process, or ``None`` on failure."""
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT
    with _REDIS_CLIENT_LOCK:
        if _REDIS_CLIENT is not None:
            return _REDIS_CLIENT
        try:
            import redis

            _REDIS_CLIENT = redis.Redis.from_url(
                getattr(settings, "REDIS_URL", "redis://redis:6379/1"),
                decode_responses=True,
                socket_connect_timeout=float(getattr(settings, "REDIS_CONNECT_TIMEOUT", 1.0)),
                socket_timeout=float(getattr(settings, "REDIS_SOCKET_TIMEOUT", 1.0)),
                health_check_interval=30,
                retry_on_timeout=True,
                max_connections=int(getattr(settings, "REDIS_MAX_CONNECTIONS", 40)),
            )
            _REDIS_CLIENT.ping()
        except Exception as exc:  # Redis failure must never take down API reads.
            logger.warning("Redis runtime helpers unavailable: %s", exc)
            _REDIS_CLIENT = None
        return _REDIS_CLIENT


def redis_get(key: str) -> str | None:
    client = get_redis_client()
    if client is None:
        return None
    try:
        return client.get(key)
    except Exception:
        return None


def redis_set(key: str, value: Any, ttl: int, *, only_if_absent: bool = False) -> bool:
    client = get_redis_client()
    if client is None:
        return False
    try:
        return bool(client.set(key, value, ex=max(1, int(ttl)), nx=only_if_absent))
    except Exception:
        return False


def redis_delete(*keys: str) -> None:
    client = get_redis_client()
    if client is None or not keys:
        return
    try:
        client.delete(*keys)
    except Exception:
        pass


def _scope_key(prefix: str, scope: str) -> str:
    digest = hashlib.sha256(scope.encode("utf-8", "ignore")).hexdigest()[:28]
    return f"sedabox:{prefix}:{digest}"


def _unique(values: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    result = []
    for value in values:
        marker = str(value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def fresh_order_ids(scope: str, ranked_ids: Sequence[Any], *, limit: int | None = None,
                    recent_window: int | None = None) -> list[Any]:
    """Order accurate ranked IDs with recently exposed IDs moved to the back.

    The original ranking remains authoritative. Redis is used only to avoid
    immediate repetition; it never invents candidates or changes eligibility.
    """
    ranked = _unique(ranked_ids)
    if not ranked:
        return []

    effective_limit = len(ranked) if limit is None else max(0, min(int(limit), len(ranked)))
    if effective_limit == 0:
        return []

    window = recent_window or min(max(effective_limit * 5, 60), max(len(ranked), 60))
    client = get_redis_client()
    if client is None:
        # Safe stateless fallback: rotate without lowering the source ranking pool.
        offset = int(time.time_ns() // 1_000_000) % len(ranked)
        rotated = ranked[offset:] + ranked[:offset]
        return rotated[:effective_limit]

    key = _scope_key("fresh", scope)
    try:
        recent = client.lrange(key, 0, max(0, window - 1))
    except Exception:
        recent = []

    recent_position = {value: index for index, value in enumerate(recent)}
    unseen = [value for value in ranked if str(value) not in recent_position]
    seen = [value for value in ranked if str(value) in recent_position]
    # Among previously seen candidates, oldest exposures come first.
    seen.sort(key=lambda value: recent_position[str(value)], reverse=True)
    return (unseen + seen)[:effective_limit]


def fresh_select_ids(scope: str, ranked_ids: Sequence[Any], *, limit: int,
                     recent_window: int | None = None,
                     ttl: int = 6 * 60 * 60) -> list[Any]:
    """Atomically select and remember a fresh, quality-ranked slice.

    Candidate eligibility and ranking always come from the recommendation engine.
    Redis only moves recently exposed candidates behind unseen candidates. Selection
    and exposure recording happen in one Lua script, preventing concurrent requests
    from receiving the same slice.
    """
    ranked = _unique(ranked_ids)
    effective_limit = max(0, min(int(limit), len(ranked)))
    if effective_limit == 0:
        return []
    window = recent_window or min(max(effective_limit * 6, 72), max(len(ranked), 72))
    client = get_redis_client()
    if client is None:
        with _LOCAL_FRESH_LOCK:
            offset = _LOCAL_FRESH_COUNTERS.get(scope, 0) % len(ranked)
            _LOCAL_FRESH_COUNTERS[scope] = offset + effective_limit
        rotated = ranked[offset:] + ranked[:offset]
        return rotated[:effective_limit]

    key = _scope_key("fresh", scope)
    script = r"""
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local recent = redis.call('LRANGE', key, 0, window - 1)
local position = {}
for i, value in ipairs(recent) do
  if position[value] == nil then position[value] = i end
end
local unseen = {}
local seen = {}
for i = 4, #ARGV do
  local value = ARGV[i]
  if position[value] == nil then
    table.insert(unseen, value)
  else
    table.insert(seen, {value=value, pos=position[value]})
  end
end
table.sort(seen, function(a, b) return a.pos > b.pos end)
local selected = {}
for _, value in ipairs(unseen) do
  if #selected >= limit then break end
  table.insert(selected, value)
end
for _, row in ipairs(seen) do
  if #selected >= limit then break end
  table.insert(selected, row.value)
end
for i = #selected, 1, -1 do redis.call('LPUSH', key, selected[i]) end
redis.call('LTRIM', key, 0, window - 1)
redis.call('EXPIRE', key, ttl)
return selected
"""
    try:
        selected = client.eval(
            script, 1, key, effective_limit, max(1, int(window)),
            max(60, int(ttl)), *[str(value) for value in ranked]
        )
        by_string = {str(value): value for value in ranked}
        return [by_string[str(value)] for value in selected if str(value) in by_string]
    except Exception:
        ordered = fresh_order_ids(scope, ranked, limit=effective_limit, recent_window=window)
        remember_exposure(scope, ordered, ttl=ttl, recent_window=window)
        return ordered


def remember_exposure(scope: str, ids: Sequence[Any], *, ttl: int = 6 * 60 * 60,
                      recent_window: int = 240) -> None:
    values = [str(value) for value in _unique(ids) if value is not None]
    if not values:
        return
    client = get_redis_client()
    if client is None:
        return
    key = _scope_key("fresh", scope)
    try:
        pipe = client.pipeline(transaction=False)
        # LPUSH reverses multiple arguments; reverse first to preserve recency order.
        pipe.lpush(key, *reversed(values))
        pipe.ltrim(key, 0, max(0, int(recent_window) - 1))
        pipe.expire(key, max(60, int(ttl)))
        pipe.execute()
    except Exception:
        pass


def fresh_order_objects(scope: str, items: Sequence[T], *, identity=lambda item: item,
                        limit: int | None = None) -> list[T]:
    object_map = {str(identity(item)): item for item in items}
    ordered_ids = fresh_order_ids(scope, list(object_map), limit=limit)
    return [object_map[item_id] for item_id in map(str, ordered_ids) if item_id in object_map]


def mark_generated_playlist_usage(items: Iterable[Any]) -> None:
    """Record lightweight last-use timestamps for persisted generated playlists."""
    ids: set[int] = set()
    for item in items:
        raw = getattr(item, "pk", item)
        try:
            item_id = int(raw)
        except (TypeError, ValueError):
            continue
        if item_id > 0:
            ids.add(item_id)
    if not ids:
        return
    client = get_redis_client()
    if client is None:
        return
    now = time.time()
    try:
        client.zadd(_USAGE_ZSET, {str(item_id): now for item_id in ids})
    except Exception:
        pass

def _recent_usage_scores(client, ids: Sequence[int]) -> list[float | None]:
    if not ids:
        return []
    try:
        # Redis >= 6.2 / redis-py >= 4.
        values = client.zmscore(_USAGE_ZSET, [str(item_id) for item_id in ids])
        return [float(value) if value is not None else None for value in values]
    except Exception:
        try:
            pipe = client.pipeline(transaction=False)
            for item_id in ids:
                pipe.zscore(_USAGE_ZSET, str(item_id))
            values = pipe.execute()
            return [float(value) if value is not None else None for value in values]
        except Exception:
            return [None] * len(ids)


def cleanup_unused_generated_playlists(*, startup: bool = False) -> dict[str, int]:
    """Delete only disposable recommendation rows unused for the configured age.

    A row is preserved permanently once it has any durable interaction: a detail
    view (``views > 0``), liked/saved/viewed users, or ``expires_at`` set to NULL.
    Redis last-use data protects rows recently returned by list/home requests.
    Cleanup is skipped when Redis is unavailable because deletion must be safe.
    """
    client = get_redis_client()
    if client is None:
        return {"deleted": 0, "candidates": 0, "skipped": 1}

    lock_token = f"{os.getpid()}:{time.time_ns()}"
    try:
        acquired = client.set(_CLEANUP_LOCK, lock_token, nx=True, ex=300)
    except Exception:
        acquired = False
    if not acquired:
        return {"deleted": 0, "candidates": 0, "skipped": 1}

    deleted_total = 0
    candidate_total = 0
    try:
        close_old_connections()
        from .models import RecommendedPlaylist

        age_seconds = max(300, int(getattr(settings, "GENERATED_PLAYLIST_UNUSED_TTL", 3600)))
        cutoff = timezone.now() - timedelta(seconds=age_seconds)
        cutoff_score = cutoff.timestamp()
        batch_size = max(100, int(getattr(settings, "GENERATED_PLAYLIST_CLEANUP_BATCH", 500)))
        cursor = 0

        generated_filter = (
            Q(unique_id__startswith='smart_rec_')
            | Q(unique_id__startswith='freshmix_')
        )
        base = RecommendedPlaylist.objects.filter(generated_filter,
            id__gt=cursor,
            expires_at__isnull=False,
            updated_at__lt=cutoff,
            views=0,
            liked_by__isnull=True,
            saved_by__isnull=True,
            viewed_by__isnull=True,
        ).distinct()

        while True:
            ids = list(
                base.filter(id__gt=cursor).order_by("id").values_list("id", flat=True)[:batch_size]
            )
            if not ids:
                break
            cursor = ids[-1]
            candidate_total += len(ids)
            scores = _recent_usage_scores(client, ids)
            stale_ids = [
                item_id for item_id, score in zip(ids, scores)
                if score is None or score < cutoff_score
            ]
            if stale_ids:
                delete_qs = RecommendedPlaylist.objects.filter(generated_filter,
                    id__in=stale_ids,
                    expires_at__isnull=False,
                    views=0,
                    liked_by__isnull=True,
                    saved_by__isnull=True,
                    viewed_by__isnull=True,
                ).distinct()
                playlist_count = delete_qs.count()
                delete_qs.delete()
                deleted_total += int(playlist_count)
                try:
                    client.zrem(_USAGE_ZSET, *[str(item_id) for item_id in stale_ids])
                except Exception:
                    pass

        # Keep the Redis usage index itself bounded.
        try:
            client.zremrangebyscore(_USAGE_ZSET, "-inf", cutoff_score)
        except Exception:
            pass

        # Safe secondary housekeeping for high-churn authentication tables.
        from .models import OtpCode, RefreshToken

        OtpCode.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()
        RefreshToken.objects.filter(
            expires_at__lt=timezone.now() - timedelta(days=7)
        ).delete()

        logger.info(
            "Generated playlist cleanup complete: startup=%s candidates=%s deleted=%s",
            startup,
            candidate_total,
            deleted_total,
        )
        return {"deleted": deleted_total, "candidates": candidate_total, "skipped": 0}
    except Exception:
        logger.exception("Generated playlist cleanup failed")
        return {"deleted": deleted_total, "candidates": candidate_total, "skipped": 1}
    finally:
        close_old_connections()
        try:
            if client.get(_CLEANUP_LOCK) == lock_token:
                client.delete(_CLEANUP_LOCK)
        except Exception:
            pass


def _maintenance_loop() -> None:
    # Redis may start a few seconds after the web container. Keep retrying without
    # blocking Gunicorn instead of silently disabling maintenance for this process.
    client = None
    while client is None:
        client = get_redis_client()
        if client is None:
            time.sleep(5)

    # One cleanup per deployment start even when Gunicorn boots several workers.
    try:
        if client.set(_STARTUP_GUARD, os.getpid(), nx=True, ex=300):
            cleanup_unused_generated_playlists(startup=True)
    except Exception:
        pass

    interval = max(300, int(getattr(settings, "GENERATED_PLAYLIST_CLEANUP_INTERVAL", 3600)))
    while True:
        time.sleep(interval)
        client = get_redis_client()
        if client is None:
            continue
        try:
            # All workers may wake up; only one receives the distributed due token.
            if not client.set(_PERIODIC_DUE, os.getpid(), nx=True, ex=max(60, interval - 10)):
                continue
        except Exception:
            continue
        cleanup_unused_generated_playlists(startup=False)


def start_generated_playlist_maintenance() -> None:
    """Start one failure-safe daemon scheduler per process.

    Redis guards ensure only one Gunicorn worker performs each startup/hourly
    cleanup cycle. Management commands are deliberately excluded.
    """
    global _MAINTENANCE_STARTED
    if not getattr(settings, "GENERATED_PLAYLIST_MAINTENANCE_ENABLED", True):
        return
    management_commands = {
        "makemigrations", "migrate", "collectstatic", "shell", "test",
        "ensure_bilingual_schema", "ensure_search_indexes",
    }
    if any(argument in management_commands for argument in sys.argv[1:]):
        return

    with _MAINTENANCE_LOCK:
        if _MAINTENANCE_STARTED:
            return
        _MAINTENANCE_STARTED = True
        thread = threading.Thread(
            target=_maintenance_loop,
            name="generated-playlist-maintenance",
            daemon=True,
        )
        thread.start()
