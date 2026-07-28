"""Redis cache backend that fails over to an in-process cache.

Redis remains the primary shared cache. A temporary Redis/DNS/network problem must
not make API requests fail or keep Gunicorn unavailable, so cache operations fall
back to Django's thread-safe local-memory backend until Redis recovers.
"""

from __future__ import annotations

import logging
import time

from django.core.cache.backends.base import DEFAULT_TIMEOUT
from django.core.cache.backends.locmem import LocMemCache
from django.core.cache.backends.redis import RedisCache

try:
    from redis.exceptions import RedisError
except Exception:  # pragma: no cover - redis is an installed project dependency.
    RedisError = Exception

logger = logging.getLogger(__name__)


class ResilientRedisCache(RedisCache):
    """Use Redis normally and degrade to per-process memory on connection errors."""

    _warning_interval = 60.0

    def __init__(self, server, params):
        super().__init__(server, params)
        fallback_location = f"sedabox-fallback:{params.get('KEY_PREFIX', 'default')}"
        self._fallback = LocMemCache(fallback_location, params)
        self._last_warning_at = 0.0

    def _warn(self, operation: str, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._last_warning_at >= self._warning_interval:
            logger.warning(
                "Redis cache unavailable during %s; using local fallback: %s",
                operation,
                exc,
            )
            self._last_warning_at = now

    def _redis_call(self, operation: str, redis_func, fallback_func):
        try:
            return redis_func()
        except (RedisError, OSError, TimeoutError) as exc:
            self._warn(operation, exc)
            return fallback_func()

    def add(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        return self._redis_call(
            "add",
            lambda: super(ResilientRedisCache, self).add(key, value, timeout, version),
            lambda: self._fallback.add(key, value, timeout, version),
        )

    def get(self, key, default=None, version=None):
        try:
            value = super().get(key, default, version)
            if value is not default:
                self._fallback.set(key, value, self.default_timeout, version)
            return value
        except (RedisError, OSError, TimeoutError) as exc:
            self._warn("get", exc)
            return self._fallback.get(key, default, version)

    def set(self, key, value, timeout=DEFAULT_TIMEOUT, version=None):
        result = self._redis_call(
            "set",
            lambda: super(ResilientRedisCache, self).set(key, value, timeout, version),
            lambda: self._fallback.set(key, value, timeout, version),
        )
        self._fallback.set(key, value, timeout, version)
        return result

    def touch(self, key, timeout=DEFAULT_TIMEOUT, version=None):
        return self._redis_call(
            "touch",
            lambda: super(ResilientRedisCache, self).touch(key, timeout, version),
            lambda: self._fallback.touch(key, timeout, version),
        )

    def delete(self, key, version=None):
        self._fallback.delete(key, version)
        return self._redis_call(
            "delete",
            lambda: super(ResilientRedisCache, self).delete(key, version),
            lambda: False,
        )

    def get_many(self, keys, version=None):
        try:
            values = super().get_many(keys, version)
            if values:
                self._fallback.set_many(values, self.default_timeout, version)
            return values
        except (RedisError, OSError, TimeoutError) as exc:
            self._warn("get_many", exc)
            return self._fallback.get_many(keys, version)

    def set_many(self, data, timeout=DEFAULT_TIMEOUT, version=None):
        self._fallback.set_many(data, timeout, version)
        return self._redis_call(
            "set_many",
            lambda: super(ResilientRedisCache, self).set_many(data, timeout, version),
            lambda: [],
        )

    def delete_many(self, keys, version=None):
        self._fallback.delete_many(keys, version)
        return self._redis_call(
            "delete_many",
            lambda: super(ResilientRedisCache, self).delete_many(keys, version),
            lambda: 0,
        )

    def clear(self):
        self._fallback.clear()
        return self._redis_call(
            "clear",
            lambda: super(ResilientRedisCache, self).clear(),
            lambda: True,
        )

    def incr(self, key, delta=1, version=None):
        return self._redis_call(
            "incr",
            lambda: super(ResilientRedisCache, self).incr(key, delta, version),
            lambda: self._fallback.incr(key, delta, version),
        )

    def has_key(self, key, version=None):
        return self._redis_call(
            "has_key",
            lambda: super(ResilientRedisCache, self).has_key(key, version),
            lambda: self._fallback.has_key(key, version),
        )

    def close(self, **kwargs):
        try:
            super().close(**kwargs)
        finally:
            self._fallback.close(**kwargs)
