"""Redis cache backend that fails open to *cache misses*, never stale local data.

Redis is the shared coordination/cache layer. A per-process value fallback is
unsafe for freshness because another Daphne worker cannot invalidate it. During
an outage reads therefore miss and callers fall back to PostgreSQL/current
computation. A small circuit breaker prevents every request from paying a
network timeout while Redis is known to be unavailable.
"""
from __future__ import annotations

import logging
import threading
import time

from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.redis import RedisCache

try:
    from redis.exceptions import RedisError
except Exception:  # pragma: no cover
    RedisError = Exception

logger = logging.getLogger(__name__)


class ResilientRedisCache(RedisCache):
    """Use Redis normally; on failure return neutral cache-miss semantics."""

    _warning_interval = 60.0
    _retry_seconds = 5.0

    def __init__(self, server, params):
        super().__init__(server, params)
        self._last_warning_at = 0.0
        self._retry_after = 0.0
        self._state_lock = threading.Lock()

    def _warn(self, operation: str, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._last_warning_at >= self._warning_interval:
            logger.warning(
                "Redis cache unavailable during %s; treating cache as a miss: %s",
                operation, exc,
            )
            self._last_warning_at = now

    def _trip(self, operation: str, exc: BaseException) -> None:
        with self._state_lock:
            self._retry_after = max(self._retry_after, time.monotonic() + self._retry_seconds)
        self._warn(operation, exc)

    def _call(self, operation: str, redis_func, fallback_func):
        if time.monotonic() < self._retry_after:
            return fallback_func()
        try:
            result = redis_func()
            self._retry_after = 0.0
            return result
        except (RedisError, OSError, TimeoutError) as exc:
            self._trip(operation, exc)
            return fallback_func()

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        # Claiming work locally is safer than making a request wait for a lock
        # that cannot exist while Redis is down. Duplicate computation is okay.
        return self._call(
            "add",
            lambda: super(ResilientRedisCache, self).add(key, value, timeout, version),
            lambda: True,
        )

    def get(self, key, default=None, version=None):
        return self._call(
            "get",
            lambda: super(ResilientRedisCache, self).get(key, default, version),
            lambda: default,
        )

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        return self._call(
            "set",
            lambda: super(ResilientRedisCache, self).set(key, value, timeout, version),
            lambda: False,
        )

    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        return self._call(
            "touch",
            lambda: super(ResilientRedisCache, self).touch(key, timeout, version),
            lambda: False,
        )

    def delete(self, key, version=None):
        return self._call(
            "delete",
            lambda: super(ResilientRedisCache, self).delete(key, version),
            lambda: False,
        )

    def get_many(self, keys, version=None):
        return self._call(
            "get_many",
            lambda: super(ResilientRedisCache, self).get_many(keys, version),
            dict,
        )

    def set_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        return self._call(
            "set_many",
            lambda: super(ResilientRedisCache, self).set_many(data, timeout, version),
            list,
        )

    def delete_many(self, keys, version=None):
        return self._call(
            "delete_many",
            lambda: super(ResilientRedisCache, self).delete_many(keys, version),
            lambda: 0,
        )

    def clear(self):
        return self._call(
            "clear",
            lambda: super(ResilientRedisCache, self).clear(),
            lambda: True,
        )

    def incr(self, key, delta=1, version=None):
        def unavailable():
            raise ConnectionError("Redis shared counter unavailable")

        return self._call(
            "incr",
            lambda: super(ResilientRedisCache, self).incr(key, delta, version),
            unavailable,
        )

    def has_key(self, key, version=None):
        return self._call(
            "has_key",
            lambda: super(ResilientRedisCache, self).has_key(key, version),
            lambda: False,
        )
