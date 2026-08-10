#!/usr/bin/env bash
set -euo pipefail

# update_app.sh
# Safely update every backend runtime process from one shared image.
# The script can update itself: after git reset, if this file changed, it re-execs
# the freshly checked-out version once so a deployment never needs a second run.
# Usage: ./deploy/update_app.sh [branch]

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_PATH="$REPO_DIR/deploy/update_app.sh"
cd "$REPO_DIR"

BRANCH="${1:-main}"
WEB_SERVICE="${WEB_SERVICE:-web}"
RUNTIME_SERVICES="${RUNTIME_SERVICES:-release_scheduler recommendation_worker runtime_maintenance ranking_worker}"
NGINX_CONTAINER="${NGINX_CONTAINER_NAME:-nginx}"
DEPLOY_REEXECED="${SEDABOX_DEPLOY_REEXECED:-0}"

if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
else
  COMPOSE_CMD="docker compose"
fi

script_hash() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$SCRIPT_PATH" 2>/dev/null | awk '{print $1}'
  else
    cksum "$SCRIPT_PATH" 2>/dev/null | awk '{print $1":"$2}'
  fi
}

SCRIPT_HASH_BEFORE="$(script_hash || true)"

echo "Repo: $REPO_DIR"
echo "Branch: $BRANCH"
echo "Web service: $WEB_SERVICE"
echo "Runtime services: $RUNTIME_SERVICES"
echo "Using compose command: $COMPOSE_CMD"

echo "Fetching latest from origin..."
git fetch origin

echo "Checking out branch $BRANCH and hard-reset to origin/$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

# A running shell may continue executing the old inode after git replaces this
# script. Re-exec the new script once so deploy-script updates take effect in the
# same deployment instead of requiring the operator to run deploy twice.
SCRIPT_HASH_AFTER="$(script_hash || true)"
if [ "$DEPLOY_REEXECED" != "1" ] && [ -n "$SCRIPT_HASH_BEFORE" ] && [ -n "$SCRIPT_HASH_AFTER" ] && [ "$SCRIPT_HASH_BEFORE" != "$SCRIPT_HASH_AFTER" ]; then
  echo "Deploy script changed in the new revision; re-executing the fresh version now..."
  exec env SEDABOX_DEPLOY_REEXECED=1 bash "$SCRIPT_PATH" "$BRANCH"
fi

echo "Generating Django migrations (makemigrations) so they are included in the image..."
set +e
if command -v python >/dev/null 2>&1; then
  echo "Running: python manage.py makemigrations --noinput"
  python manage.py makemigrations --noinput
  MSTATUS=$?
else
  echo "Host python not found; attempting ephemeral docker container to run makemigrations"
  if command -v docker >/dev/null 2>&1; then
    docker run --rm -v "$REPO_DIR":/app -w /app python:3.11-slim bash -eux -c \
      "apt-get update && apt-get install -y gcc libpq-dev build-essential || true; \
       pip install --no-cache-dir -r requirements.txt; \
       python manage.py makemigrations --noinput"
    MSTATUS=$?
  else
    echo "No docker available to run makemigrations; skipping makemigrations." >&2
    MSTATUS=0
  fi
fi
set -e
if [ "$MSTATUS" -ne 0 ]; then
  echo "makemigrations returned non-zero status $MSTATUS. The script will continue, but migrations may be missing." >&2
else
  echo "makemigrations completed (exit $MSTATUS)."
fi

echo "Stopping backend runtime services before schema/static work"
set +e
# shellcheck disable=SC2086
$COMPOSE_CMD stop "$WEB_SERVICE" $RUNTIME_SERVICES >/dev/null 2>&1
# shellcheck disable=SC2086
$COMPOSE_CMD rm -f "$WEB_SERVICE" $RUNTIME_SERVICES >/dev/null 2>&1
set -e

echo "Building shared backend image"
$COMPOSE_CMD build "$WEB_SERVICE"

echo "Running migrations from the freshly built image"
$COMPOSE_CMD run --rm --no-deps "$WEB_SERVICE" python manage.py migrate --noinput

echo "Collecting static files from the freshly built image"
$COMPOSE_CMD run --rm --no-deps "$WEB_SERVICE" python manage.py collectstatic --noinput

echo "Starting web and runtime workers on the same revision"
# shellcheck disable=SC2086
$COMPOSE_CMD up -d --no-deps --force-recreate "$WEB_SERVICE" $RUNTIME_SERVICES

# Prove the new Daphne/Django process answers HTTP before switching/reloading the
# reverse proxy. 404/401/403 are valid application responses; 5xx/connection
# failures mean the backend is not ready.
WEB_CONTAINER_ID="$($COMPOSE_CMD ps -q "$WEB_SERVICE")"
if [ -z "$WEB_CONTAINER_ID" ]; then
  echo "Unable to resolve the new web container id." >&2
  exit 1
fi

echo "Waiting for Django HTTP readiness inside the new web container..."
WEB_READY=0
for _ in $(seq 1 60); do
  if docker exec "$WEB_CONTAINER_ID" python -c 'import urllib.error, urllib.request
try:
    r = urllib.request.urlopen("http://127.0.0.1:8000/", timeout=2)
    code = r.getcode()
except urllib.error.HTTPError as exc:
    code = exc.code
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if code < 500 else 1)' >/dev/null 2>&1; then
    WEB_READY=1
    break
  fi
  sleep 1
done

if [ "$WEB_READY" != "1" ]; then
  echo "New web container never became HTTP-ready. Recent logs:" >&2
  $COMPOSE_CMD logs --no-color --tail=200 "$WEB_SERVICE" >&2 || true
  exit 1
fi

echo "Django is HTTP-ready. Refreshing Nginx upstream resolution..."
if docker inspect "$NGINX_CONTAINER" >/dev/null 2>&1; then
  if [ "$(docker inspect -f '{{.State.Running}}' "$NGINX_CONTAINER" 2>/dev/null || true)" != "true" ]; then
    echo "Nginx container '$NGINX_CONTAINER' exists but is not running." >&2
    exit 1
  fi

  # `nginx -t` parses proxy_pass hostnames again. This verifies Docker DNS can
  # resolve the newly recreated soundbox_web before a graceful reload replaces
  # the old upstream address in active Nginx workers.
  docker exec "$NGINX_CONTAINER" nginx -t
  docker exec "$NGINX_CONTAINER" nginx -s reload
  echo "Nginx reloaded successfully."
else
  echo "Nginx container '$NGINX_CONTAINER' was not found; skipping proxy reload." >&2
fi

echo "Deployment finished. Showing recent logs for the web service (last 200 lines):"
$COMPOSE_CMD logs --no-color --tail=200 "$WEB_SERVICE"

echo "Done."
