#!/bin/sh
set -eu

is_true() {
    value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
    [ "$value" = "1" ] || [ "$value" = "true" ] || [ "$value" = "yes" ] || [ "$value" = "on" ]
}

if [ -n "${MEDIA_ROOT:-}" ]; then
    mkdir -p "$MEDIA_ROOT"
fi

if [ -n "${DATABASE_URL:-}" ]; then
    echo "Waiting for PostgreSQL..."
    python - <<'PY'
import os
import time

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "payroll.settings")

import django
from django.db import connections
from django.db.utils import DatabaseError

django.setup()

for attempt in range(1, 31):
    try:
        connections["default"].ensure_connection()
        print("PostgreSQL is available.")
        break
    except DatabaseError as exc:
        if attempt == 30:
            raise
        print(f"Database unavailable ({attempt}/30): {exc}")
        time.sleep(2)
PY
fi

if is_true "${MIGRATE_ON_START:-true}"; then
    echo "Applying database migrations..."
    python manage.py migrate --noinput
fi

if is_true "${COLLECTSTATIC_ON_START:-false}"; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput
fi

exec "$@"
