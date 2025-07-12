#docker-entrypoint.sh
#!/usr/bin/env bash
set -euo pipefail

echo "⏳ Waiting for Postgres to be ready…"
until pg_isready -h db -U "${POSTGRES_USER:-a3ddev}" -d "${POSTGRES_DB:-galaxybee}" >/dev/null 2>&1; do
  sleep 1
done
echo "✅ Postgres is up!"

echo "🔄 Applying Alembic migrations…"

alembic -c /app/alembic.ini upgrade head

if [ $# -eq 0 ]; then
  echo "❌ No command specified!"
  exit 1
fi

case "$1" in
  *.py)
    echo "🚀 Starting bot…"
    exec python -u "$@"
    ;;
  celery|-A*)
    echo "🚀 Starting Celery…"
    exec "$@"
    ;;
  *)
    echo "❌ Unknown command: $@"
    exit 1
    ;;
esac