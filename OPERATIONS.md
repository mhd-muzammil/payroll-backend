# Payroll Production Operations

All commands in this document are for the Dokploy Ubuntu VPS, run from the
checked-out `payroll_backend` Compose project directory unless stated
otherwise.

## Service Status

```bash
docker compose ps
docker compose logs --tail=200 backend
docker compose logs --tail=200 database
docker compose exec backend python manage.py check --deploy
docker compose exec backend python manage.py showmigrations
```

Health endpoints:

| Endpoint | Purpose |
| --- | --- |
| Backend `/livez/` | Django process liveness |
| Backend `/healthz/` | Django and database readiness |
| Frontend `/healthz` | Nginx liveness |

## Deploying A New Revision

1. Back up PostgreSQL and media.
2. Deploy the backend revision first.
3. Confirm `/healthz/` and migration status.
4. Deploy the frontend revision.
5. Complete the post-deployment checklist in `DEPLOYMENT.md`.

The backend entrypoint applies pending migrations before Gunicorn starts.
For a manual migration check:

```bash
docker compose exec backend python manage.py migrate --plan
docker compose exec backend python manage.py migrate --noinput
```

## PostgreSQL Backup

Load Compose environment values and create a compressed logical backup:

```bash
set -a
. ./.env
set +a
mkdir -p backups
docker compose exec -T database \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc \
  > "backups/payroll-$(date +%Y%m%d-%H%M%S).dump"
```

Copy backups off the VPS to encrypted storage. A volume snapshot alone is not
a sufficient database backup.

## Media Backup

The Compose project name is fixed to `payroll`, so the media volume is
`payroll_media_data`.

```bash
mkdir -p backups
docker run --rm \
  -v payroll_media_data:/data:ro \
  -v "$PWD/backups":/backup \
  alpine:3.23 \
  tar -czf "/backup/media-$(date +%Y%m%d-%H%M%S).tar.gz" -C /data .
```

Employee documents contain sensitive personal and financial information.
Encrypt backups, restrict access, and define a retention policy.

## PostgreSQL Restore

Restoring replaces current database contents. Stop the backend first:

```bash
set -a
. ./.env
set +a
docker compose stop backend
docker compose exec -T database \
  dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB"
docker compose exec -T database \
  createdb -U "$POSTGRES_USER" "$POSTGRES_DB"
docker compose exec -T database \
  pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists \
  < backups/payroll-YYYYMMDD-HHMMSS.dump
docker compose start backend
curl --fail https://api.payroll.example.com/healthz/
```

## Media Restore

Stop the backend before replacing media:

```bash
docker compose stop backend
docker run --rm \
  -v payroll_media_data:/data \
  -v "$PWD/backups":/backup:ro \
  alpine:3.23 \
  sh -c 'find /data -mindepth 1 -delete && tar -xzf /backup/media-YYYYMMDD-HHMMSS.tar.gz -C /data'
docker compose start backend
```

Confirm document download through the authenticated API after restore.

## Application Rollback

1. In Dokploy, select the last known-good Git revision for backend and frontend.
2. Redeploy the backend revision.
3. Check `docker compose logs backend` and `/healthz/`.
4. Redeploy the matching frontend revision.
5. Repeat functional verification.

Application rollback does not automatically reverse database migrations.
Django migrations should normally move forward with a corrective migration.
If an incompatible migration must be reversed, restore the pre-deployment
database backup and matching media backup before deploying the old code.

## Log Management

Both Compose services use the Docker `json-file` driver with 10 MB rotation and
three retained files. Application and access logs go to stdout/stderr:

```bash
docker compose logs -f --tail=200 backend
docker compose logs -f --tail=200 database
```

Configure Dokploy log shipping or a VPS collector for long-term retention.
Never log JWTs, passwords, bank account values, or uploaded document content.

## Common Failures

### Backend stays unhealthy

```bash
docker compose logs --tail=200 backend
docker compose exec backend python manage.py check
docker compose exec backend python manage.py showmigrations
docker compose exec database pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

Check `DATABASE_URL`, database credentials, volume permissions, and pending
migration errors.

### `DisallowedHost`

Add the exact API hostname used by Dokploy/Nginx to `ALLOWED_HOSTS`. If the
frontend proxy targets an internal service hostname, include that hostname too.

### CSRF or CORS errors

Use full HTTPS origins, including scheme and without paths:

```text
CSRF_TRUSTED_ORIGINS=https://api.payroll.example.com,https://payroll.example.com
CORS_ALLOWED_ORIGINS=https://payroll.example.com
```

### Redirect loop behind Dokploy

Confirm Dokploy forwards `X-Forwarded-Proto: https`. Django trusts that header
through `SECURE_PROXY_SSL_HEADER`.

### Frontend returns 502 for `/api/`

Verify `BACKEND_UPSTREAM` includes `http://` or `https://`, resolves from the
frontend container, and has no path suffix:

```bash
docker logs <frontend-container>
```

### Upload rejected

Allowed extensions are PDF, JPG, JPEG, PNG, WEBP, DOC, and DOCX. The extension,
reported MIME type, and file signature must agree. The default limit is 10 MB
per file and can be changed with `MAX_UPLOAD_SIZE_MB`.

### Uploaded file exists but cannot be opened

Check the `media_data` volume is mounted at `/app/media`, the backend container
can read it, and the request uses the authenticated document API. Raw
`/media/...` URLs are intentionally unavailable.

### SPA route returns 404

Confirm the frontend is using the repository `nginx.conf`. Nginx must fall back
to `/index.html` for routes that are not physical files.

## Security Checklist

- `DEBUG=False`.
- Strong unique `SECRET_KEY`.
- PostgreSQL has no public port or domain.
- Dokploy provides HTTPS for both domains.
- Raw `/media/` is inaccessible.
- Document requests require a valid JWT.
- Database and media backups are encrypted and tested.
- Secrets exist only in Dokploy/environment configuration.
- Containers run as non-root with `no-new-privileges`.
- Images and dependencies are rebuilt regularly for security updates.
