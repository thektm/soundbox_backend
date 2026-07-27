#!/bin/sh
set -eu

python - <<'PY'
import os, socket, time
host = os.getenv('DB_HOST')
port = int(os.getenv('DB_PORT', '5432'))
if host:
    for _ in range(60):
        try:
            with socket.create_connection((host, port), timeout=2):
                break
        except OSError:
            time.sleep(1)
    else:
        raise SystemExit(f'database {host}:{port} is unavailable')
PY

python manage.py ensure_guest_preview_schema
[ "${ENSURE_SEARCH_INDEXES_ON_STARTUP:-1}" = "1" ] && python manage.py ensure_search_indexes || true

if [ "${GENERATE_PREVIEWS_ON_STARTUP:-0}" = "1" ]; then
    python manage.py generate_missing_previews --limit "${PREVIEW_STARTUP_LIMIT:-50}" || true
fi

exec "$@"
