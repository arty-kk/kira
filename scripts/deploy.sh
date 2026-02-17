#!/usr/bin/env bash
set -euo pipefail

DEPLOY_PATH=${DEPLOY_PATH:-}
DEPLOY_BRANCH=${DEPLOY_BRANCH:-main}
DEPLOY_REPO_URL=${DEPLOY_REPO_URL:-}
API_SCALE=${API_SCALE:-}
DIALOG_WORKER_SCALE=${DIALOG_WORKER_SCALE:-}
RAG_WORKER_SCALE=${RAG_WORKER_SCALE:-}
AUDIT_WORKER_SCALE=${AUDIT_WORKER_SCALE:-}
MAINTENANCE_WORKER_SCALE=${MAINTENANCE_WORKER_SCALE:-}
POSTGRES_SCALE=${POSTGRES_SCALE:-}
REDIS_SCALE=${REDIS_SCALE:-}
MIGRATE_SCALE=${MIGRATE_SCALE:-}

if [[ -z "${DEPLOY_PATH}" ]]; then
  echo "DEPLOY_PATH is required" >&2
  exit 1
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

echo "Checking out ${DEPLOY_BRANCH} from origin..."
git checkout -B "${DEPLOY_BRANCH}" "origin/${DEPLOY_BRANCH}"

echo "Current branch and status:"
git branch -v
git status

docker compose down

docker compose build --no-cache --pull

scale_args=()
if [[ -n "${POSTGRES_SCALE}" ]]; then
  scale_args+=(--scale postgres="${POSTGRES_SCALE}")
fi
if [[ -n "${REDIS_SCALE}" ]]; then
  scale_args+=(--scale redis="${REDIS_SCALE}")
fi
if [[ -n "${MIGRATE_SCALE}" ]]; then
  scale_args+=(--scale migrate="${MIGRATE_SCALE}")
fi
if [[ -n "${API_SCALE}" ]]; then
  scale_args+=(--scale api="${API_SCALE}")
fi
if [[ -n "${DIALOG_WORKER_SCALE}" ]]; then
  scale_args+=(--scale dialog_worker="${DIALOG_WORKER_SCALE}")
fi
if [[ -n "${RAG_WORKER_SCALE}" ]]; then
  scale_args+=(--scale rag_worker="${RAG_WORKER_SCALE}")
fi
if [[ -n "${AUDIT_WORKER_SCALE}" ]]; then
  scale_args+=(--scale audit_worker="${AUDIT_WORKER_SCALE}")
fi
if [[ -n "${MAINTENANCE_WORKER_SCALE}" ]]; then
  scale_args+=(--scale maintenance_worker="${MAINTENANCE_WORKER_SCALE}")
fi

docker compose up -d --force-recreate "${scale_args[@]}"

docker system prune -a --volumes -f
