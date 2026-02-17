# Deploy script contract (`scripts/deploy.sh`)

`scripts/deploy.sh` accepts only current scale variables that map 1:1 to Docker Compose services.

## Required variables

- `DEPLOY_PATH` — absolute path to checked out project on deploy host.

## Optional variables

- `DEPLOY_BRANCH` (default: `main`)
- `DEPLOY_REPO_URL` (used when the repository is not cloned yet)
- `COMPOSE_SCALE_OVERRIDES` — list like `api=2,worker-tasks=3;redis_kv=1`

## Supported scale variables

- `DB_SCALE` → `db`
- `REDIS_KV_SCALE` → `redis_kv`
- `REDIS_VEC_SCALE` → `redis_vec`
- `MIGRATE_SCALE` → `migrate`
- `API_SCALE` → `api`
- `WORKER_TASKS_SCALE` → `worker-tasks`
- `WORKER_MODERATION_SCALE` → `worker-moderation`
- `WORKER_MEDIA_SCALE` → `worker-media`
- `WORKER_QUEUE_SCALE` → `worker-queue`
- `WORKER_API_SCALE` → `worker-api`

All scale values must be non-negative integers.

## Validation behavior

The script exits with a non-zero code if:

- required variables are empty,
- scale values are not non-negative integers,
- `COMPOSE_SCALE_OVERRIDES` contains bad `key=value` pairs,
- `COMPOSE_SCALE_OVERRIDES` references unknown services,
- any legacy scale variable is set (`DIALOG_WORKER_SCALE`, `RAG_WORKER_SCALE`, `AUDIT_WORKER_SCALE`, `MAINTENANCE_WORKER_SCALE`, `POSTGRES_SCALE`, `REDIS_SCALE`).

Use `DEPLOY_VALIDATE_ONLY=1` to validate inputs and generated `--scale` arguments without running `git` or `docker` commands.
