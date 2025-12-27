#!/usr/bin/env bash
set -e

# Wait for DB if DB_HOST is provided (simple wait loop)
if [ -n "$DB_HOST" ]; then
  echo "Checking database connection to $DB_HOST:$DB_PORT..."
  until python -c "import sys, psycopg2; psycopg2.connect(dbname='$DB_NAME', user='$DB_USER', password='$DB_PASSWORD', host='$DB_HOST', port='$DB_PORT'); print('DB available')" 2>/dev/null; do
    echo "Waiting for Postgres at $DB_HOST:$DB_PORT..."
    sleep 1
  done
fi

# Run migrations and collectstatic
python manage.py makemigrations --noinput
python manage.py migrate --noinput
python manage.py collectstatic --noinput

# Start the process
exec "$@"
