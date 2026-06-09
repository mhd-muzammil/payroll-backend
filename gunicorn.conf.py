import multiprocessing
import os


bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("GUNICORN_WORKERS", min(multiprocessing.cpu_count() + 1, 4)))
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "2"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "60"))
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", "50"))
worker_tmp_dir = "/dev/shm"
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
capture_output = True
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "*")
access_log_format = (
    'remote=%(h)s method="%(m)s" path="%(U)s" query="%(q)s" '
    'status=%(s)s bytes=%(B)s duration_us=%(D)s referer="%(f)s" agent="%(a)s"'
)
