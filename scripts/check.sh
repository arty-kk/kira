#!/usr/bin/env bash
set -euo pipefail

export OPENAI_API_KEY="${OPENAI_API_KEY:-test}"
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-123456:ABCdef1234567890}"
export TELEGRAM_BOT_USERNAME="${TELEGRAM_BOT_USERNAME:-testbot}"
export TELEGRAM_BOT_ID="${TELEGRAM_BOT_ID:-1}"
export WEBHOOK_URL="${WEBHOOK_URL:-https://example.invalid}"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://user:pass@localhost/db}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}"
export REDIS_URL_QUEUE="${REDIS_URL_QUEUE:-redis://localhost:6379/1}"
export REDIS_URL_VECTOR="${REDIS_URL_VECTOR:-redis://localhost:6379/2}"
export CELERY_BROKER_URL="${CELERY_BROKER_URL:-redis://localhost:6379/3}"
export TWITTER_API_KEY="${TWITTER_API_KEY:-test}"
export TWITTER_API_SECRET="${TWITTER_API_SECRET:-test}"
export TWITTER_ACCESS_TOKEN="${TWITTER_ACCESS_TOKEN:-test}"
export TWITTER_ACCESS_TOKEN_SECRET="${TWITTER_ACCESS_TOKEN_SECRET:-test}"
export TWITTER_BEARER_TOKEN="${TWITTER_BEARER_TOKEN:-test}"

echo "[check] pytest"
python -m pytest -q

echo "[check] compileall"
python -m compileall app

has_ruff_config=false
if [ -f "pyproject.toml" ] || [ -f "ruff.toml" ] || [ -f ".ruff.toml" ]; then
  has_ruff_config=true
fi

if [ "$has_ruff_config" = true ]; then
  if command -v ruff >/dev/null 2>&1; then
    echo "[check] ruff"
    ruff check .
  elif [ "${CI:-}" = "true" ]; then
    echo "[check] ERROR: ruff config found (pyproject.toml/ruff.toml/.ruff.toml), but ruff is not installed in CI"
    exit 1
  else
    echo "[check] ruff skipped (ruff config found, but ruff is not installed)"
  fi
else
  echo "[check] ruff skipped (no ruff config file)"
fi

if [ -f "pyrightconfig.json" ]; then
  if command -v pyright >/dev/null 2>&1; then
    echo "[check] pyright"
    pyright
  elif [ "${CI:-}" = "true" ]; then
    echo "[check] ERROR: pyrightconfig.json found, but pyright is not installed in CI"
    exit 1
  else
    echo "[check] pyright skipped (pyrightconfig.json found, but pyright is not installed)"
  fi
else
  echo "[check] pyright skipped (no pyrightconfig.json)"
fi
