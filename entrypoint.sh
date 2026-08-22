#!/bin/sh
set -eu

python - <<'PY'
import os
import socket
import time

import redis


def wait_for_tcp(label: str, host: str, port: int, timeout_seconds: int) -> None:
    deadline = time.monotonic() + max(1, timeout_seconds)
    last_error = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                print(f'{label} {host}:{port} is ready', flush=True)
                return
        except OSError as exc:
            last_error = exc
            time.sleep(1)
    raise SystemExit(f'{label} {host}:{port} is unavailable: {last_error}')


def wait_for_redis(label: str, value: str, timeout_seconds: int, required: bool):
    value = value.strip()
    if not value:
        if required:
            raise SystemExit(f'{label} URL is required but empty')
        return None

    deadline = time.monotonic() + max(1, timeout_seconds)
    last_error = None
    while time.monotonic() < deadline:
        try:
            client = redis.Redis.from_url(
                value,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            if client.ping():
                print(f'{label} is ready', flush=True)
                return value
        except Exception as exc:
            last_error = exc
        time.sleep(1)

    message = f'{label} is unavailable: {last_error}'
    if required:
        raise SystemExit(message)
    print(f'{message}; continuing because {label} is optional', flush=True)
    return None


def enabled(name: str, default: str = '1') -> bool:
    return os.getenv(name, default).lower() in {'1', 'true', 'yes', 'on'}


db_host = os.getenv('DB_HOST')
if db_host:
    wait_for_tcp(
        'database',
        db_host,
        int(os.getenv('DB_PORT', '5432')),
        int(os.getenv('DB_STARTUP_WAIT_SECONDS', '60')),
    )

redis_url = os.getenv('REDIS_URL', '').strip()
ready_cache_url = wait_for_redis(
    'redis cache',
    redis_url,
    int(os.getenv('REDIS_STARTUP_WAIT_SECONDS', '60')),
    enabled('REDIS_REQUIRED_ON_STARTUP', '1'),
)

# Channels may use a different Redis database, credentials, TLS mode, or host.
# PING the exact URL so ASGI cannot start with a healthy TCP port but a broken
# authentication/database configuration.
channel_url = os.getenv('CHANNEL_REDIS_URL', redis_url).strip()
if channel_url and channel_url == ready_cache_url:
    print('channels redis is ready', flush=True)
else:
    wait_for_redis(
        'channels redis',
        channel_url,
        int(os.getenv('CHANNEL_REDIS_STARTUP_WAIT_SECONDS', '60')),
        enabled('CHANNEL_REDIS_REQUIRED_ON_STARTUP', '1'),
    )
PY


# Schema/index maintenance runs once in the web service. Other worker services
# use the same image but must not repeat startup DDL.
case " $* " in
  *" run_daphne_cluster.py "*)
    [ "${ENSURE_SEARCH_INDEXES_ON_STARTUP:-1}" = "1" ] && python manage.py ensure_search_indexes || true
    [ "${ENSURE_PLAYLIST_UNIQUE_IDS_ON_STARTUP:-1}" = "1" ] && python manage.py ensure_playlist_unique_ids || true
    ;;
esac

exec "$@"
