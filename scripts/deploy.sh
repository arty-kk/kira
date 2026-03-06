#!/usr/bin/env bash
set -euo pipefail

DEPLOY_PATH=${DEPLOY_PATH:-}
DEPLOY_BRANCH=${DEPLOY_BRANCH:-main}
DEPLOY_REPO_URL=${DEPLOY_REPO_URL:-}
DEPLOY_COMMIT_SHA=${DEPLOY_COMMIT_SHA:-}

DB_SCALE=${DB_SCALE:-}
REDIS_KV_SCALE=${REDIS_KV_SCALE:-}
REDIS_VEC_SCALE=${REDIS_VEC_SCALE:-}
MIGRATE_SCALE=${MIGRATE_SCALE:-}
API_SCALE=${API_SCALE:-}
WORKER_TASKS_SCALE=${WORKER_TASKS_SCALE:-}
WORKER_MODERATION_SCALE=${WORKER_MODERATION_SCALE:-}
WORKER_MEDIA_SCALE=${WORKER_MEDIA_SCALE:-}
WORKER_QUEUE_SCALE=${WORKER_QUEUE_SCALE:-}
WORKER_API_SCALE=${WORKER_API_SCALE:-}
BOOTSTRAP_RAG_SCALE=${BOOTSTRAP_RAG_SCALE:-}
COMPOSE_SCALE_OVERRIDES=${COMPOSE_SCALE_OVERRIDES:-}

BOOTSTRAP_RAG_MAX_ATTEMPTS=${BOOTSTRAP_RAG_MAX_ATTEMPTS:-3}
BOOTSTRAP_RAG_RETRY_DELAY_SEC=${BOOTSTRAP_RAG_RETRY_DELAY_SEC:-10}

readonly LEGACY_SCALE_VARS=(
  DIALOG_WORKER_SCALE
  RAG_WORKER_SCALE
  AUDIT_WORKER_SCALE
  MAINTENANCE_WORKER_SCALE
  POSTGRES_SCALE
  REDIS_SCALE
)

readonly SCALE_WHITELIST=(
  db
  redis_kv
  redis_vec
  migrate
  bootstrap-rag
  api
  worker-tasks
  worker-moderation
  worker-media
  worker-queue
  worker-api
)

declare -A scale_values

is_whitelisted_service() {
  local candidate=$1
  local service
  for service in "${SCALE_WHITELIST[@]}"; do
    if [[ "${service}" == "${candidate}" ]]; then
      return 0
    fi
  done
  return 1
}

require_non_empty() {
  local name=$1
  local value=$2
  if [[ -z "${value}" ]]; then
    echo "${name} is required and must be non-empty" >&2
    exit 1
  fi
}

validate_scale_value() {
  local name=$1
  local value=$2
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "${name} must be a non-negative integer, got: ${value}" >&2
    exit 1
  fi
}

require_gate_scale_enabled() {
  local service=$1
  local env_name=$2
  local value=${scale_values[${service}]:-}

  if [[ -n "${value}" && "${value}" == "0" ]]; then
    echo "${env_name}=0 is not allowed: ${service} is a mandatory deploy gate" >&2
    exit 1
  fi
}

set_scale_if_present() {
  local service=$1
  local env_name=$2
  local env_value=$3

  if [[ -z "${env_value}" ]]; then
    return
  fi

  validate_scale_value "${env_name}" "${env_value}"
  scale_values["${service}"]="${env_value}"
}

validate_no_legacy_scale_vars() {
  local name
  for name in "${LEGACY_SCALE_VARS[@]}"; do
    if [[ -n "${!name:-}" ]]; then
      echo "${name} is no longer supported; use the current scale variables for compose service names" >&2
      exit 1
    fi
  done
}

parse_compose_scale_overrides() {
  local raw_overrides=$1
  local normalized
  local pair
  local service
  local value

  if [[ -z "${raw_overrides}" ]]; then
    return
  fi

  normalized=$(echo "${raw_overrides}" | tr ',' '\n' | tr ';' '\n')

  while IFS= read -r pair; do
    pair=$(echo "${pair}" | xargs)
    [[ -z "${pair}" ]] && continue

    if [[ "${pair}" != *=* ]]; then
      echo "COMPOSE_SCALE_OVERRIDES entry must be in <service>=<int> format: ${pair}" >&2
      exit 1
    fi

    service=${pair%%=*}
    value=${pair#*=}
    service=$(echo "${service}" | xargs)
    value=$(echo "${value}" | xargs)

    if ! is_whitelisted_service "${service}"; then
      echo "COMPOSE_SCALE_OVERRIDES contains unknown service: ${service}" >&2
      exit 1
    fi

    validate_scale_value "COMPOSE_SCALE_OVERRIDES(${service})" "${value}"
    scale_values["${service}"]="${value}"
  done <<< "${normalized}"
}

build_scale_args() {
  local service
  local scale_args_ref_name=$1
  local -n scale_args_ref="${scale_args_ref_name}"

  for service in "${SCALE_WHITELIST[@]}"; do
    if [[ -n "${scale_values[${service}]:-}" ]]; then
      scale_args_ref+=(--scale "${service}=${scale_values[${service}]}")
    fi
  done
}

require_non_empty "DEPLOY_PATH" "${DEPLOY_PATH}"
validate_no_legacy_scale_vars

set_scale_if_present "db" "DB_SCALE" "${DB_SCALE}"
set_scale_if_present "redis_kv" "REDIS_KV_SCALE" "${REDIS_KV_SCALE}"
set_scale_if_present "redis_vec" "REDIS_VEC_SCALE" "${REDIS_VEC_SCALE}"
set_scale_if_present "migrate" "MIGRATE_SCALE" "${MIGRATE_SCALE}"
set_scale_if_present "bootstrap-rag" "BOOTSTRAP_RAG_SCALE" "${BOOTSTRAP_RAG_SCALE}"
set_scale_if_present "api" "API_SCALE" "${API_SCALE}"
set_scale_if_present "worker-tasks" "WORKER_TASKS_SCALE" "${WORKER_TASKS_SCALE}"
set_scale_if_present "worker-moderation" "WORKER_MODERATION_SCALE" "${WORKER_MODERATION_SCALE}"
set_scale_if_present "worker-media" "WORKER_MEDIA_SCALE" "${WORKER_MEDIA_SCALE}"
set_scale_if_present "worker-queue" "WORKER_QUEUE_SCALE" "${WORKER_QUEUE_SCALE}"
set_scale_if_present "worker-api" "WORKER_API_SCALE" "${WORKER_API_SCALE}"
parse_compose_scale_overrides "${COMPOSE_SCALE_OVERRIDES}"
require_gate_scale_enabled "migrate" "MIGRATE_SCALE"
require_gate_scale_enabled "bootstrap-rag" "BOOTSTRAP_RAG_SCALE"

scale_args=()
build_scale_args scale_args

if [[ "${DEPLOY_VALIDATE_ONLY:-0}" == "1" ]]; then
  printf 'Validated scale args:'
  if [[ ${#scale_args[@]} -eq 0 ]]; then
    printf ' <none>'
  else
    printf ' %s' "${scale_args[@]}"
  fi
  printf '\n'
  exit 0
fi

if [[ -d "${DEPLOY_PATH}/.git" ]]; then
  cd "${DEPLOY_PATH}"
elif [[ -n "${DEPLOY_REPO_URL}" ]]; then
  if [[ -e "${DEPLOY_PATH}" && -n "$(ls -A "${DEPLOY_PATH}")" ]]; then
    echo "DEPLOY_PATH exists but is not empty and does not contain a git repository: ${DEPLOY_PATH}" >&2
    exit 1
  fi
  mkdir -p "${DEPLOY_PATH}"
  git clone --branch "${DEPLOY_BRANCH}" --single-branch "${DEPLOY_REPO_URL}" "${DEPLOY_PATH}"
  cd "${DEPLOY_PATH}"
else
  echo "DEPLOY_PATH does not contain a git repository: ${DEPLOY_PATH}" >&2
  echo "Set DEPLOY_REPO_URL to allow deploy.sh to clone the repository automatically." >&2
  exit 1
fi

echo "Fetching from origin..."
git fetch --prune origin

if [[ -n "${DEPLOY_COMMIT_SHA}" ]]; then
  echo "Deploy target: commit ${DEPLOY_COMMIT_SHA}"
  if ! git cat-file -e "${DEPLOY_COMMIT_SHA}^{commit}"; then
    echo "DEPLOY_COMMIT_SHA was provided but commit is not available after fetch: ${DEPLOY_COMMIT_SHA}" >&2
    exit 1
  fi

  echo "Checking out commit ${DEPLOY_COMMIT_SHA} in detached HEAD mode..."
  git checkout --detach "${DEPLOY_COMMIT_SHA}"
else
  echo "Deploy target: branch ${DEPLOY_BRANCH}"
  echo "Checking out ${DEPLOY_BRANCH} from origin..."
  git checkout -B "${DEPLOY_BRANCH}" "origin/${DEPLOY_BRANCH}"
fi

echo "Current branch and status:"
git branch -v
git status

docker compose build --no-cache --pull

echo "Running migrate gate (schema must be ready before rollout)..."
docker compose up --force-recreate --abort-on-container-exit --exit-code-from migrate migrate
echo "MIGRATIONS_DONE"

echo "Running post-migrate DB smoke-check gate..."
docker compose run --rm migrate sh -lc 'test -f scripts/check_db_state.py || { echo "scripts/check_db_state.py not found in container workdir" >&2; exit 1; }'
docker compose run --rm migrate python scripts/check_db_state.py
echo "DB_SMOKE_CHECK_DONE"

validate_scale_value "BOOTSTRAP_RAG_MAX_ATTEMPTS" "${BOOTSTRAP_RAG_MAX_ATTEMPTS}"
validate_scale_value "BOOTSTRAP_RAG_RETRY_DELAY_SEC" "${BOOTSTRAP_RAG_RETRY_DELAY_SEC}"

bootstrap_attempt=1
while (( bootstrap_attempt <= BOOTSTRAP_RAG_MAX_ATTEMPTS )); do
  echo "Running RAG bootstrap attempt ${bootstrap_attempt}/${BOOTSTRAP_RAG_MAX_ATTEMPTS}..."
  if docker compose up --force-recreate --abort-on-container-exit --exit-code-from bootstrap-rag bootstrap-rag; then
    echo "RAG_BOOTSTRAP_DONE"
    break
  fi

  echo "RAG_BOOTSTRAP_FAILED_ATTEMPT ${bootstrap_attempt}/${BOOTSTRAP_RAG_MAX_ATTEMPTS}" >&2
  if (( bootstrap_attempt == BOOTSTRAP_RAG_MAX_ATTEMPTS )); then
    echo "RAG_BOOTSTRAP_FAILED_FINAL" >&2
    exit 1
  fi

  sleep "${BOOTSTRAP_RAG_RETRY_DELAY_SEC}"
  bootstrap_attempt=$((bootstrap_attempt + 1))
done

docker compose up -d --force-recreate "${scale_args[@]}"

docker system prune -a --volumes -f
