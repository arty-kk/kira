#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
DEPLOY_SCRIPT="${REPO_ROOT}/scripts/deploy.sh"

run_and_assert_ok() {
  local name=$1
  shift
  if ! output=$("$@" 2>&1); then
    echo "[FAIL] ${name}: expected success" >&2
    echo "${output}" >&2
    exit 1
  fi
}

run_and_assert_fail() {
  local name=$1
  shift
  if output=$("$@" 2>&1); then
    echo "[FAIL] ${name}: expected failure" >&2
    echo "${output}" >&2
    exit 1
  fi
}

run_and_assert_ok "explicit scales" env \
  DEPLOY_PATH="${REPO_ROOT}" \
  DEPLOY_VALIDATE_ONLY=1 \
  DB_SCALE=1 \
  REDIS_KV_SCALE=2 \
  REDIS_VEC_SCALE=3 \
  API_SCALE=4 \
  MIGRATE_SCALE=1 \
  BOOTSTRAP_RAG_SCALE=1 \
  WORKER_TASKS_SCALE=5 \
  WORKER_MODERATION_SCALE=6 \
  WORKER_MEDIA_SCALE=7 \
  WORKER_QUEUE_SCALE=8 \
  WORKER_API_SCALE=9 \
  bash "${DEPLOY_SCRIPT}"

run_and_assert_ok "override list" env \
  DEPLOY_PATH="${REPO_ROOT}" \
  DEPLOY_VALIDATE_ONLY=1 \
  COMPOSE_SCALE_OVERRIDES="api=3,worker-tasks=2;redis_kv=1" \
  bash "${DEPLOY_SCRIPT}"

run_and_assert_fail "legacy variables rejected" env \
  DEPLOY_PATH="${REPO_ROOT}" \
  DEPLOY_VALIDATE_ONLY=1 \
  POSTGRES_SCALE=1 \
  bash "${DEPLOY_SCRIPT}"

run_and_assert_fail "unknown override service rejected" env \
  DEPLOY_PATH="${REPO_ROOT}" \
  DEPLOY_VALIDATE_ONLY=1 \
  COMPOSE_SCALE_OVERRIDES="unknown=1" \
  bash "${DEPLOY_SCRIPT}"

run_and_assert_fail "migrate gate cannot be disabled" env \
  DEPLOY_PATH="${REPO_ROOT}" \
  DEPLOY_VALIDATE_ONLY=1 \
  MIGRATE_SCALE=0 \
  bash "${DEPLOY_SCRIPT}"

run_and_assert_fail "bootstrap gate cannot be disabled" env \
  DEPLOY_PATH="${REPO_ROOT}" \
  DEPLOY_VALIDATE_ONLY=1 \
  BOOTSTRAP_RAG_SCALE=0 \
  bash "${DEPLOY_SCRIPT}"

run_and_assert_fail "negative scale rejected" env \
  DEPLOY_PATH="${REPO_ROOT}" \
  DEPLOY_VALIDATE_ONLY=1 \
  DB_SCALE=-1 \
  bash "${DEPLOY_SCRIPT}"

run_and_assert_fail "empty deploy path rejected" env \
  DEPLOY_VALIDATE_ONLY=1 \
  DB_SCALE=1 \
  bash "${DEPLOY_SCRIPT}"

echo "[OK] deploy scale validation checks passed"
