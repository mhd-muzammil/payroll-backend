# Payroll Production Deployment

## Architecture

```text
Internet
   |
   v
Dokploy / Traefik (DNS, TLS certificates, HTTPS redirect)
   |
   +--> payroll.example.com
   |       |
   |       v
   |    Frontend container
   |    Nginx :8080 (non-root)
   |       |
   |       +--> React SPA and immutable static assets
   |       +--> /api/* proxy
   |                 |
   |                 v
   +--> api.payroll.example.com
           |
           v
        Backend container
        Gunicorn + Django :8000 (non-root)
           |
           +--> PostgreSQL 18 (private Compose network)
           +--> media_data volume (private employee documents)
```

The workspace contains two independent Git repositories:

- `payroll_backend`: Django API, PostgreSQL Compose service, migrations, and media volume.
- `payroll_frontend`: Vite/React build and Nginx runtime.

Deploy them as two Dokploy applications. The backend repository uses Docker
Compose so PostgreSQL and Django share a private network. The frontend
repository uses its Dockerfile and proxies `/api/` to the backend domain.

## Runtime Inventory

| Component | Production runtime |
| --- | --- |
| Backend | Python 3.11, Django 5.2, Django REST Framework |
| App server | Gunicorn with `gthread` workers |
| Database | PostgreSQL 18 |
| Frontend build | Node.js 22 and npm |
| Frontend runtime | Nginx on port 8080 |
| Authentication | JWT access and refresh tokens |
| Persistent storage | PostgreSQL volume and private media volume |
| Background workers | None |
| Redis/cache | None |

## Required Environment Variables

### Backend Compose application

| Variable | Required | Example |
| --- | --- | --- |
| `POSTGRES_DB` | Yes | `payroll` |
| `POSTGRES_USER` | Yes | `payroll` |
| `POSTGRES_PASSWORD` | Yes | URL-safe random password |
| `SECRET_KEY` | Yes | 50+ random characters |
| `ALLOWED_HOSTS` | Yes | `api.payroll.example.com` |
| `CSRF_TRUSTED_ORIGINS` | Yes | `https://api.payroll.example.com,https://payroll.example.com` |
| `CORS_ALLOWED_ORIGINS` | Yes | `https://payroll.example.com` |

Recommended values are in `.env.example`. `DATABASE_URL` is assembled inside
Compose. When Django is run outside Compose, set a complete PostgreSQL URL:

```text
postgresql://payroll:password@database-host:5432/payroll?sslmode=require
```

If `DATABASE_URL` is absent, Django retains the existing SQLite development
behavior and honors `SQLITE_PATH`.

### Frontend application

| Variable | Required | Example |
| --- | --- | --- |
| `BACKEND_UPSTREAM` | Yes | `https://api.payroll.example.com` |
| `VITE_API_BASE_URL` | No | Empty for same-origin `/api` proxy |

`BACKEND_UPSTREAM` is a runtime Nginx value. Keep `VITE_API_BASE_URL` empty in
production unless the browser must call the API domain directly.

## Local Production Build

### Backend and PostgreSQL

From `payroll_backend`:

```bash
cp .env.example .env
# Replace every placeholder in .env.
docker compose config
docker compose build --pull backend
docker compose up -d
docker compose ps
docker compose exec backend python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=3)"
```

The Compose service intentionally publishes no host ports. For local-only
testing, temporarily use:

```bash
docker compose run --rm --service-ports -p 8000:8000 backend
```

Do not add a public PostgreSQL port.

### Frontend

From `payroll_frontend`:

```bash
docker build --pull -t payroll-frontend .
docker run --rm -p 8080:8080 \
  -e BACKEND_UPSTREAM=https://api.payroll.example.com \
  payroll-frontend
curl --fail http://127.0.0.1:8080/healthz
```

## Dokploy Deployment

### 1. Backend Compose application

1. Create a Dokploy **Compose** application from the `payroll_backend` Git repository.
2. Set Compose path to `docker-compose.yml`.
3. Add all backend environment variables listed above.
4. Enable Dokploy isolated deployments, or confirm in **Preview Compose** that
   Dokploy attaches its routing network to service `backend`.
5. Deploy the Compose project.
6. Add domain `api.payroll.example.com` to service `backend`, container port `8000`.
7. Enable HTTPS and certificate management in Dokploy.
8. Set the health path to `/healthz/`.
9. Do not add a domain or public port to service `database`.

The backend entrypoint waits for PostgreSQL and runs `python manage.py migrate
--noinput` on every start. Django migrations are transactional where the
database backend supports them and are safe to rerun.

### 2. Frontend Dockerfile application

1. Create a Dokploy **Application** from the `payroll_frontend` Git repository.
2. Select Dockerfile build and use `Dockerfile`.
3. Set `BACKEND_UPSTREAM=https://api.payroll.example.com`.
4. Leave `VITE_API_BASE_URL` empty.
5. Add domain `payroll.example.com`, container port `8080`.
6. Enable HTTPS and certificate management.
7. Set the health path to `/healthz`.

### 3. DNS

Point both hostnames to the Dokploy VPS:

```text
payroll.example.com      A/AAAA -> VPS
api.payroll.example.com  A/AAAA -> VPS
```

## SQLite Data Transfer

The repository SQLite database is excluded from container images. For an
existing installation, export data before switching production to PostgreSQL.

On the source system:

```bash
python manage.py dumpdata \
  --natural-foreign \
  --natural-primary \
  --exclude contenttypes \
  --exclude auth.permission \
  --exclude sessions \
  --indent 2 \
  --output payroll-data.json
```

On the Dokploy VPS, from the backend Compose project directory:

```bash
docker compose cp payroll-data.json backend:/tmp/payroll-data.json
docker compose exec backend python manage.py loaddata /tmp/payroll-data.json
docker compose exec backend python manage.py check
```

Copy the existing media directory into the `payroll_media_data` volume
separately. Do not place employee documents in the image or a public web root.

## Post-Deployment Verification

```bash
curl --fail https://api.payroll.example.com/livez/
curl --fail https://api.payroll.example.com/healthz/
curl --fail https://payroll.example.com/healthz
```

Then verify:

1. Login returns access and refresh tokens.
2. Dashboard, employees, attendance, leave, payroll, and payslip APIs load.
3. A permitted PDF or image uploads successfully.
4. A raw `/media/...` request returns 404.
5. An anonymous protected document request returns 401.
6. An authenticated document opens through `/api/onboarding/{id}/documents/{field}/`.
7. SPA routes survive a browser refresh.
8. PostgreSQL and media volumes remain after container recreation.

## Build And Run Commands

Backend Compose:

```bash
docker compose build --pull backend
docker compose up -d
```

Frontend:

```bash
docker build --pull -t payroll-frontend .
docker run -d --restart unless-stopped -p 8080:8080 \
  -e BACKEND_UPSTREAM=https://api.payroll.example.com \
  payroll-frontend
```

Dokploy performs equivalent build and run operations from the configured Git
revisions.
