#!/bin/sh
set -eu

python - <<'PY'
import os
import socket
import time
from urllib.parse import urlparse


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


db_host = os.getenv('DB_HOST')
if db_host:
    wait_for_tcp(
        'database',
        db_host,
        int(os.getenv('DB_PORT', '5432')),
        int(os.getenv('DB_STARTUP_WAIT_SECONDS', '60')),
    )

redis_url = os.getenv('REDIS_URL', '').strip()
redis_required = os.getenv('REDIS_REQUIRED_ON_STARTUP', '1').lower() in {
    '1', 'true', 'yes', 'on'
}
if redis_url:
    parsed = urlparse(redis_url)
    redis_host = parsed.hostname
    redis_port = parsed.port or 6379
    if not redis_host:
        raise SystemExit(f'invalid REDIS_URL: {redis_url!r}')
    try:
        wait_for_tcp(
            'redis',
            redis_host,
            redis_port,
            int(os.getenv('REDIS_STARTUP_WAIT_SECONDS', '5')),
        )
    except SystemExit as exc:
        if redis_required:
            raise
        print(f'{exc}; continuing with resilient local cache fallback', flush=True)
PY

python manage.py ensure_guest_preview_schema
[ "${ENSURE_SEARCH_INDEXES_ON_STARTUP:-1}" = "1" ] && python manage.py ensure_search_indexes || true

if [ "${GENERATE_PREVIEWS_ON_STARTUP:-0}" = "1" ]; then
    python manage.py generate_missing_previews --limit "${PREVIEW_STARTUP_LIMIT:-50}" || true
fi

exec "$@"
