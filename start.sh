#!/usr/bin/env bash
set -o errexit

if [ "${DEBUG:-False}" = "False" ] && [ -z "$SECRET_KEY" ]; then
  echo "ERROR: SECRET_KEY is required when DEBUG=False. Add SECRET_KEY in Render Environment Variables."
  exit 1
fi

if [ -n "$SQLITE_PATH" ]; then
  mkdir -p "$(dirname "$SQLITE_PATH")"
fi

if [ -n "$MEDIA_ROOT" ]; then
  mkdir -p "$MEDIA_ROOT"
fi

python manage.py migrate --no-input
exec gunicorn payroll.wsgi:application --bind 0.0.0.0:"$PORT" --access-logfile - --error-logfile -
