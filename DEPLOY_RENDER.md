# Render Deployment

Use `payroll_backend` as the Render root directory.

## Render service settings

- Runtime: Python 3
- Build Command: `bash build.sh`
- Start Command: `bash start.sh`
- Health Check Path: `/healthz/`

## Required environment variables

- `SECRET_KEY`: generate a strong secret in Render
- `SQLITE_PATH`: `/var/data/payroll/db.sqlite3`
- `MEDIA_ROOT`: `/var/data/payroll/media`
- `DEBUG`: `False`
- `ALLOWED_HOSTS`: your Render hostname and custom API domain, comma-separated
- `CSRF_TRUSTED_ORIGINS`: trusted HTTPS origins, comma-separated
- `CORS_ALLOWED_ORIGINS`: frontend origins allowed to call the API, comma-separated

## SQLite and uploaded documents

This app uses SQLite and stores onboarding documents with Django `FileField`s. Render instances have ephemeral filesystems unless you add a persistent disk.

Use a paid Render service with one persistent disk mounted at:

`/var/data/payroll`

The blueprint sets:

- `SQLITE_PATH=/var/data/payroll/db.sqlite3`
- `MEDIA_ROOT=/var/data/payroll/media`

SQLite on Render should run with one service instance only. Do not scale this service horizontally while SQLite is the database.

Migrations run in `start.sh`, not `build.sh`, because Render persistent disks are available at runtime, not during the build command.

`build.sh` uses a temporary build-only `SECRET_KEY` if Render does not expose your runtime secret during the build phase. You still must set a real `SECRET_KEY` in Render for runtime.

If Render still selects the wrong Python version, set this environment variable in Render:

`PYTHON_VERSION=3.11.13`

When creating the service manually in the Render dashboard, `render.yaml` does not automatically create generated env vars for that existing manual service. Add `SECRET_KEY` yourself under Environment Variables.
