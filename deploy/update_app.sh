#!/usr/bin/env bash
set -euo pipefail

# update_app.sh
# Safely update only the web app: fetch from GitHub, hard-reset to origin, rebuild web service,
# recreate container and run migrations + collectstatic.
# Usage: ./update_app.sh [branch]
# - branch: git branch to deploy (default: main)
# - set WEB_SERVICE env var to the service name in docker-compose (default: web)

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

BRANCH="${1:-main}"
WEB_SERVICE="${WEB_SERVICE:-web}"

echo "Repo: $REPO_DIR"
echo "Branch: $BRANCH"
echo "Web service: $WEB_SERVICE"

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

echo "Stopping and removing existing web container (if any)"
set +e
$COMPOSE_CMD stop "$WEB_SERVICE" >/dev/null 2>&1
$COMPOSE_CMD rm -f "$WEB_SERVICE" >/dev/null 2>&1
set -e

echo "Building web service image (no cache)"
$COMPOSE_CMD build --no-cache "$WEB_SERVICE"

echo "Starting fresh web container"
# recreate only web service without dependencies
$COMPOSE_CMD up -d --no-deps --force-recreate "$WEB_SERVICE"

echo "Waiting a few seconds for the container to initialize..."
sleep 4

echo "Running migrations inside web container"
# use -T to avoid tty issues in non-interactive scripts
$COMPOSE_CMD exec -T "$WEB_SERVICE" python manage.py migrate --noinput || {
  echo "Warning: migrations failed to run via compose exec; attempting docker exec by container name"
  # fallback: find container name and exec into it
  CONTAINER_NAME=$(docker ps --filter "name=${WEB_SERVICE}" --format "{{.Names}}" | head -n1)
  if [ -n "$CONTAINER_NAME" ]; then
    docker exec -i "$CONTAINER_NAME" python manage.py migrate --noinput
  else
    echo "Could not find running container for service $WEB_SERVICE to run migrations." >&2
    exit 1
  fi
}

echo "Collecting static files"
$COMPOSE_CMD exec -T "$WEB_SERVICE" python manage.py collectstatic --noinput || {
  echo "collectstatic failed via compose exec; attempting docker exec by container name"
  CONTAINER_NAME=$(docker ps --filter "name=${WEB_SERVICE}" --format "{{.Names}}" | head -n1)
  if [ -n "$CONTAINER_NAME" ]; then
    docker exec -i "$CONTAINER_NAME" python manage.py collectstatic --noinput
  else
    echo "Could not find running container for service $WEB_SERVICE to run collectstatic." >&2
    exit 1
  fi
}

echo "Deployment finished. Showing recent logs for the web service (last 200 lines):"
$COMPOSE_CMD logs --no-color --tail=200 "$WEB_SERVICE"

echo "Done. If you are using nginx, ensure it is running and proxying to the web service."
