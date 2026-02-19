# Deploy script contract (`scripts/deploy.sh`)

`scripts/deploy.sh` accepts only current scale variables that map 1:1 to Docker Compose services.

## Required variables

- `DEPLOY_PATH` — absolute path to checked out project on deploy host.

## Optional variables

- `DEPLOY_BRANCH` (default: `main`)
- `DEPLOY_REPO_URL` (used when the repository is not cloned yet)
- `DEPLOY_COMMIT_SHA` (when set, deploys exactly this commit SHA)
- `COMPOSE_SCALE_OVERRIDES` — list like `api=2,worker-tasks=3;redis_kv=1`

## first deploy/bootstrap

CD step `Deploy via SSH` follows this bootstrap order:

1. Checks `${DEPLOY_PATH}/scripts/deploy.sh`.
2. If the script is missing and `DEPLOY_REPO_URL` is set, performs bootstrap clone with `DEPLOY_BRANCH`.
3. If the script is missing and `DEPLOY_REPO_URL` is empty, fails with explicit diagnostic.

Minimum secrets for first deploy/bootstrap:

- `DEPLOY_PATH`
- `DEPLOY_BRANCH`
- `DEPLOY_REPO_URL`

Expected bootstrap result in the same CD step:

- repository appears in `DEPLOY_PATH`,
- `scripts/deploy.sh` is found,
- `scripts/deploy.sh` is executed immediately.

## Deploy target priority

- If `DEPLOY_COMMIT_SHA` is set, deployment is pinned to that exact commit (`git checkout --detach <sha>`).
- If `DEPLOY_COMMIT_SHA` is empty (for example, manual `workflow_dispatch`), the script falls back to branch deployment via `DEPLOY_BRANCH`.
- In automatic CD (`workflow_run` after successful CI), the workflow passes `github.event.workflow_run.head_sha`, so production deploys the exact commit that passed CI checks.


## Alembic migrations: minimal env

For migration-only runs (`migrate` service or manual `alembic upgrade head`), only database context is required.

Minimum env:

- `DATABASE_URL`

`REDIS_URL`, `REDIS_URL_QUEUE`, and `REDIS_URL_VECTOR` are not required to initialize Alembic migration environment.

Example:

```bash
DATABASE_URL=postgresql+asyncpg://user:pass@db:5432/appdb \
python -m alembic -c alembic/alembic.ini upgrade head
```

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

## Celery worker tuning

`worker-tasks`, `worker-media`, and `worker-moderation` read queue/concurrency/prefetch values from `.env`.
This allows changing worker tuning without rebuilding images: update `.env` and run deploy.

### What can be tuned via `.env`

- Queue routing:
  - `CELERY_DEFAULT_QUEUE` → base queue for `worker-tasks`.
  - `CELERY_MEDIA_QUEUE` → queue for `worker-media`.
  - `CELERY_MODERATION_QUEUE` → queue for `worker-moderation`.
- In-container worker behavior:
  - `CELERY_TASKS_CONCURRENCY`, `CELERY_TASKS_PREFETCH` (+ optional `CELERY_TASKS_POOL`, `CELERY_TASKS_LOGLEVEL`).
  - `CELERY_MEDIA_CONCURRENCY`, `CELERY_MEDIA_PREFETCH`.
  - `CELERY_MODERATION_CONCURRENCY`, `CELERY_MODERATION_PREFETCH`.

`CELERY_BROKER_URL` already has a compose default via `redis_kv` for current services, but it can still be overridden through `.env`.

### How this combines with scale variables

- `WORKER_*_SCALE` and `COMPOSE_SCALE_OVERRIDES` control the **number of containers** (`docker compose --scale ...`).
- `*_CONCURRENCY`/`*_PREFETCH` control **how each container consumes tasks internally**.

Effective throughput depends on both dimensions: container count × per-container tuning.

### Operational recipe

#### Profile 1: low-latency / fairness

Use when minimizing head-of-line blocking and preserving fairer task distribution is a priority.

```env
CELERY_TASKS_CONCURRENCY=6
CELERY_TASKS_PREFETCH=1
CELERY_MEDIA_CONCURRENCY=2
CELERY_MEDIA_PREFETCH=1
CELERY_MODERATION_CONCURRENCY=2
CELERY_MODERATION_PREFETCH=1
```

```bash
WORKER_TASKS_SCALE=2 \
WORKER_MEDIA_SCALE=1 \
WORKER_MODERATION_SCALE=1 \
bash scripts/deploy.sh
```

#### Profile 2: high-throughput CPU-light

Use for lightweight tasks (I/O-bound or short CPU-light jobs) where throughput is the main goal.

```env
CELERY_TASKS_CONCURRENCY=20
CELERY_TASKS_PREFETCH=2
CELERY_MEDIA_CONCURRENCY=6
CELERY_MEDIA_PREFETCH=2
CELERY_MODERATION_CONCURRENCY=4
CELERY_MODERATION_PREFETCH=2
```

```bash
COMPOSE_SCALE_OVERRIDES="worker-tasks=3,worker-media=2,worker-moderation=2" \
bash scripts/deploy.sh
```

Prefetch trade-off: higher `prefetch` can improve throughput, but may reduce fairness and increase latency for individual tasks. Start from `1`, then increase gradually if needed.

### Minimal copy-paste example (queues + deploy)

```env
CELERY_DEFAULT_QUEUE=celery
CELERY_MEDIA_QUEUE=queue_media
CELERY_MODERATION_QUEUE=queue_moderation
CELERY_TASKS_CONCURRENCY=10
CELERY_TASKS_PREFETCH=1
CELERY_MEDIA_CONCURRENCY=2
CELERY_MEDIA_PREFETCH=1
CELERY_MODERATION_CONCURRENCY=2
CELERY_MODERATION_PREFETCH=1
```

```bash
WORKER_TASKS_SCALE=2 \
WORKER_MEDIA_SCALE=1 \
WORKER_MODERATION_SCALE=1 \
bash scripts/deploy.sh
```
