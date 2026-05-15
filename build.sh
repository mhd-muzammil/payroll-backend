#!/usr/bin/env bash
set -o errexit

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

export SECRET_KEY="${SECRET_KEY:-render-build-only-secret-key-not-used-at-runtime-9LXnV3e2sHq7}"

python manage.py collectstatic --no-input
