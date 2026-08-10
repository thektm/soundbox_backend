#!/usr/bin/env bash
set -euo pipefail

# update_app.sh
# Safely update the backend runtime: fetch/reset, build one shared backend image,
# recreate the web service, run migrations/static collection, then recreate background workers.
# Usage: ./update_app.sh [branch]
# - branch: git branch to deploy (default: main)
# - WEB_SERVICE selects the HTTP/ASGI service (default: web)
# - RUNTIME_SERVICES selects services that must run the same freshly built image

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

BRANCH="${1:-main}"
WEB_SERVICE="${WEB_SERVICE:-web}"
RUNTIME_SERVICES="${RUNTIME_SERVICES:-release_scheduler recommendation_worker runtime_maintenance ranking_worker}"

echo "Repo: $REPO_DIR"
echo "Branch: $BRANCH"
echo "Web service: $WEB_SERVICE"
echo "Runtime services: $RUNTIME_SERVICES"

# choose compose command
if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
else
  COMPOSE_CMD="docker compose"
fi

echo "Using compose command: $COMPOSE_CMD"

echo "Fetching latest from origin..."
git fetch origin

echo "Checking out branch $BRANCH and hard-reset to origin/$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

echo "Generating Django migrations (makemigrations) so they are included in the image..."
# try to run makemigrations on host Python; if not available, try an ephemeral docker container
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
# Do not let old worker code keep writing while the freshly built revision is
# applying schema changes. This also prevents the new web process from serving
# requests before migrations and static collection complete.
set +e
# shellcheck disable=SC2086
$COMPOSE_CMD stop "$WEB_SERVICE" $RUNTIME_SERVICES >/dev/null 2>&1
# shellcheck disable=SC2086
$COMPOSE_CMD rm -f "$WEB_SERVICE" $RUNTIME_SERVICES >/dev/null 2>&1
set -e

echo "Building shared backend image"
# Docker's content-addressed layer cache is safe here: git was hard-reset to the
# requested revision, so changed source/dependencies invalidate the right layers
# without wasting CPU rebuilding unchanged OS/Python dependencies.
$COMPOSE_CMD build "$WEB_SERVICE"

echo "Running migrations from the freshly built image"
$COMPOSE_CMD run --rm --no-deps "$WEB_SERVICE" python manage.py migrate --noinput

echo "Collecting static files from the freshly built image"
$COMPOSE_CMD run --rm --no-deps "$WEB_SERVICE" python manage.py collectstatic --noinput

echo "Starting web and runtime workers on the same revision"
# shellcheck disable=SC2086
$COMPOSE_CMD up -d --no-deps --force-recreate "$WEB_SERVICE" $RUNTIME_SERVICES

echo "Deployment finished. Showing recent logs for the web service (last 200 lines):"
$COMPOSE_CMD logs --no-color --tail=200 "$WEB_SERVICE"

echo "Done. If you are using nginx, ensure it is running and proxying to the web service."
