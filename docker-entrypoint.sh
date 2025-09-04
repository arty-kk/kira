#!/usr/bin/env bash
set -euo pipefail

export PGDATABASE="${POSTGRES_DB}"
export PGUSER="${POSTGRES_USER}"
export PGPASSWORD="${POSTGRES_PASSWORD}"
export PGHOST="db"
export PGPORT="5432"

echo "⏳ Waiting for Postgres to be ready…"
until pg_isready -h db -U ${POSTGRES_USER} -d ${POSTGRES_DB} >/dev/null 2>&1; do
  sleep 1
done
echo "✅ Postgres is up!"

echo "🔄 Applying Alembic migrations…"
if [ "${ENABLE_SCHEDULER:-false}" = "true" ]; then
  echo "🔄 Applying Alembic migrations (scheduler mode)…"
  python -m alembic -c alembic.ini upgrade head
else
  echo "⏭ Skipping migrations (worker mode)…"
fi

if [ $# -eq 0 ]; then
  echo "❌ No command specified!"
  exit 1
fi

case "$1" in
  *.py)
    echo "🚀 Starting Python script…"
    exec python -u "$@"
    ;;
  celery)
    echo "🚀 Starting Celery worker…"
    exec "$@"
    ;;
  -A*)
    echo "🚀 Starting Celery with custom args…"
    exec celery "$@"
    ;;
  *)
    echo "❌ Unknown command: $@"
    exit 1
    ;;
esac
