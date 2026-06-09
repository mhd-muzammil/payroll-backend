FROM python:3.11.15-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt


FROM python:3.11.15-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PORT=8000

RUN groupadd --gid 10001 payroll \
    && useradd --uid 10001 --gid payroll --create-home --shell /usr/sbin/nologin payroll

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=payroll:payroll . .

RUN mkdir -p /app/media /app/staticfiles \
    && chown -R payroll:payroll /app \
    && SECRET_KEY=container-build-only DEBUG=False python manage.py collectstatic --noinput

USER payroll

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=3)" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
CMD ["gunicorn", "--config", "gunicorn.conf.py", "payroll.wsgi:application"]
