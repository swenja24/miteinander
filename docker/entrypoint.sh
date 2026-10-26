#!/bin/sh
set -eu

if [ "${1:-}" = "gunicorn" ]; then
  python manage.py migrate --noinput
  if [ -s /app/data/familie.json ]; then
    python manage.py import_legacy_json /app/data/familie.json
  fi
  python manage.py bootstrap_miteinander
  python manage.py collectstatic --noinput
fi

exec "$@"
