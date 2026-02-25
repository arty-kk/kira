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

## Migration gate in `scripts/deploy.sh`

Before starting services with `docker compose up -d`, the deploy script runs a mandatory migrate gate:

```bash
docker compose run --rm migrate
```

Expected behavior:
- If migrate exits with code `0`, deploy continues to service startup.
- If migrate exits non-zero, deploy stops immediately and `worker-*` are not started.
- If `migrate` scale is explicitly set to `0`, the gate is skipped intentionally.

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

## Troubleshooting runbook: DB sync check for `worker-tasks` after deploy

Use this sequence when `worker-tasks` reports missing DB objects (for example `refund_outbox`) after deployment.

### 1) Verify `DATABASE_URL` parity between `migrate` and `worker-tasks`

Run and capture both values:

```bash
docker compose run --rm migrate sh -lc 'printf "migrate DATABASE_URL=%s\n" "$DATABASE_URL"'
docker compose run --rm worker-tasks sh -lc 'printf "worker-tasks DATABASE_URL=%s\n" "$DATABASE_URL"'
```

OK:
- Both commands print non-empty `DATABASE_URL` values.
- Values are exactly identical as strings (same instance/host, port, database, and schema-related parameters).

Deviation means:
- Any difference means `migrate` and `worker-tasks` can target different DB context.
- Stop here, align env/compose config, and re-run this step until values match exactly.

### 2) Run migrations exactly as in `migrate`

Run migrations in `migrate` service image/container context:

```bash
docker compose run --rm migrate sh -lc 'alembic -c /app/alembic/alembic.ini upgrade head'
```

OK:
- Command exits with code `0`.
- Output has no Alembic or PostgreSQL errors.

Deviation means:
- Non-zero exit code or stack trace means migrations are not applied.
- Treat deploy as blocked: do not start/restart `worker-*` until this command succeeds.

### 3) Check `refund_outbox` in PostgreSQL target schema and verify Alembic state

Run SQL diagnostics against the same `DATABASE_URL` that `migrate` uses:

```bash
docker compose run --rm migrate sh -lc '
python - <<"PY"
import os
import sqlalchemy as sa

engine = sa.create_engine(os.environ["DATABASE_URL"])
with engine.connect() as conn:
    search_path = conn.execute(sa.text("show search_path")).scalar()
    current_schema = conn.execute(sa.text("select current_schema()")).scalar()
    in_current = conn.execute(sa.text("select to_regclass(current_schema() || '.refund_outbox') is not null")).scalar()
    in_public = conn.execute(sa.text("select to_regclass('public.refund_outbox') is not null")).scalar()
    versions = conn.execute(sa.text("select version_num from alembic_version order by version_num")).fetchall()

print(f"search_path={search_path}")
print(f"current_schema={current_schema}")
print(f"refund_outbox_in_current_schema={in_current}")
print(f"refund_outbox_in_public={in_public}")
print(f"alembic_version={versions}")
PY'
```

OK:
- `refund_outbox_in_current_schema=True`.
- `alembic_version` is readable and shows expected revision chain for current release.

Deviation means:
- `refund_outbox_in_current_schema=False` with `refund_outbox_in_public=True` means schema/search_path mismatch: worker reads another schema.
- `refund_outbox_in_current_schema=False` and `refund_outbox_in_public=False` means migration is not applied to target DB.
- Missing/incorrect `alembic_version` means wrong DB target or broken migration state; do not continue to worker restart.

### 4) Restart `worker-tasks` only after successful migration and table checks

Run only when steps 1–3 are all OK:

```bash
docker compose up -d --no-deps --force-recreate worker-tasks
```

OK:
- `worker-tasks` starts without `UndefinedTableError: relation "refund_outbox" does not exist`.

Deviation means:
- If the error persists, repeat steps 1 and 3 immediately; this usually means worker runtime still points to another DB/schema.

### 5) Enforce post-deploy gate: `migrate` must succeed before any `worker-*` starts

Apply this gate in every deploy sequence:

```bash
docker compose up migrate
docker compose ps migrate
docker compose up -d worker-tasks worker-media worker-moderation worker-queue worker-api
```

OK:
- `docker compose ps migrate` shows successful completion state (`Exited (0)`) before worker startup.

Deviation means:
- If `migrate` failed, stop worker rollout, run `docker compose logs migrate`, fix root cause, and re-run step 2.
- If worker points to another DB, align `DATABASE_URL` for both `migrate` and all `worker-*`, then restart from step 1.

## RAG bootstrap

- In docker-compose local deployment, `db` uses `pgvector/pgvector:pg16` so the `vector` extension is available for migrations.
- `migrate` now performs Alembic migration and immediately builds the system RAG tag index into PostgreSQL (`pgvector`) from `knowledge_on.json`.
- Runtime services (`api`, `main`, `worker-*`) start only after `migrate` completes, and read tag vectors from PostgreSQL tables (tags-based retrieval only).
- Legacy local JSON/NPZ vector files are not used in runtime path.

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
