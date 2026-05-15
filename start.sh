#!/usr/bin/env bash
set -o errexit

echo "Starting payroll backend..."

DEBUG_VALUE="$(printf '%s' "${DEBUG:-False}" | tr '[:upper:]' '[:lower:]')"

if [ "$DEBUG_VALUE" = "false" ] && [ -z "$SECRET_KEY" ]; then
  echo "ERROR: SECRET_KEY is required when DEBUG=False. Add SECRET_KEY in Render Environment Variables."
  exit 1
fi

if [ -z "$PORT" ]; then
  echo "ERROR: PORT is not set. Render should provide PORT automatically for web services."
  exit 1
fi

if [ -n "$SQLITE_PATH" ]; then
  echo "Preparing SQLite directory: $(dirname "$SQLITE_PATH")"
  mkdir -p "$(dirname "$SQLITE_PATH")"
else
  echo "SQLITE_PATH is not set; Django will use the local db.sqlite3 fallback."
fi

if [ -n "$MEDIA_ROOT" ]; then
  echo "Preparing media directory: $MEDIA_ROOT"
  mkdir -p "$MEDIA_ROOT"
else
  echo "MEDIA_ROOT is not set; Django will use the local media fallback."
fi

echo "Running database migrations..."
python manage.py migrate --no-input

echo "Starting Gunicorn..."
exec gunicorn payroll.wsgi:application --bind 0.0.0.0:"$PORT" --access-logfile - --error-logfile -
