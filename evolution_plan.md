# Evolution Plan

## 0. Baseline (from audit)
- Architecture map:
  - Entry points: `main.py` simultaneously starts bot/API/scheduler, while Celery workers initialize RAG state on startup (`main.py:79-137#main`, `app/tasks/celery_app.py:117-128#_warm_up_worker`).
  - RAG runtime split:
    - Global knowledge vectors are loaded from local `data/embeddings/*.npz|*.json` (`app/services/responder/rag/knowledge_proc.py:24-31#_npz_path`, `app/services/responder/rag/knowledge_proc.py:67-73#_load_precomputed`).
    - API-key scoped knowledge vectors are also loaded from local NPZ under owner directories (`app/services/responder/rag/api_kb_proc.py:21-35#_npz_path`, `app/services/responder/rag/api_kb_proc.py:153-179#_ensure_state`).
    - Keyword/tag prefilter uses local TAGS NPZ and can fallback to runtime embedding from JSON (`app/services/responder/rag/keyword_filter.py:413-420#_load_precomputed_tags_index`, `app/services/responder/rag/keyword_filter.py:456-476#_ensure_index`).
  - Build pipeline:
    - Docker has a dedicated `precompute-embeddings` service writing JSON into shared `/app/data` volume and all runtime services depend on it (`docker-compose.yml:107-122`, `docker-compose.yml:154-159`, `docker-compose.yml:206-214`).
    - API-key KB rebuild task writes JSON + NPZ + TAGS NPZ to disk and then marks DB record ready (`app/tasks/kb.py:269-342#_rebuild_for_api_key_async`, `app/tasks/kb.py:366-482#_rebuild_for_api_key_async`).
  - DB boundary today: metadata/status stored in `api_key_knowledge`, but vectors are not stored in DB (`app/core/models.py:161-183#ApiKeyKnowledge`, `alembic/versions/0001_initial_schema.py:61-77#upgrade`).
- Critical flows:
  1. Container bootstrap runs precompute job and mounts shared embedding files before bot/api/worker start (`docker-compose.yml:107-122`, `docker-compose.yml:154-159`, `docker-compose.yml:206-214`).
  2. Celery worker warm-up loads global KB into memory from local files (`app/tasks/celery_app.py:117-128#_warm_up_worker`, `app/services/responder/rag/knowledge_proc.py:190-216#_init_kb`).
  3. API request enqueues job with `knowledge_owner_id=api_key_id` for scoped RAG (`app/api/conversation.py:936-948`).
  4. Relevance gate calls tag-hit search first (`app/services/responder/rag/relevance.py:49-66#is_relevant`).
  5. Tag-hit search loads system + owner indices, embeds query, scores, MMR-selects (`app/services/responder/rag/keyword_filter.py:604-617#find_tag_hits`, `app/services/responder/rag/keyword_filter.py:710-799#find_tag_hits`).
  6. API-key KB rebuild embeds content/tags and writes NPZ artifacts (`app/tasks/kb.py:234-342#_rebuild_for_api_key_async`, `app/tasks/kb.py:366-462#_rebuild_for_api_key_async`).
  7. Orphan file GC deletes owner embedding directories on disk (`app/tasks/kb.py:560-616#_gc_orphan_api_key_dirs_async`).
- Current pain points:
  - **P0 data-source split**: DB status (`ready`) and actual vectors are separate systems (DB + filesystem), allowing state divergence (`app/core/models.py:171-175#ApiKeyKnowledge`, `app/tasks/kb.py:476-482#_rebuild_for_api_key_async`, `app/services/responder/rag/api_kb_proc.py:144-154#_ensure_state`).
  - **P0 platform mismatch with target**: no pgvector schema/usage exists; vector operations are file+NumPy only (`alembic/versions/0001_initial_schema.py:61-77#upgrade`, `app/services/responder/rag/knowledge_proc.py:98-137#_load_state_from_npz`, `app/services/responder/rag/keyword_filter.py:413-420#_load_precomputed_tags_index`).
  - **P1 hot-path risk**: when TAGS NPZ is absent, runtime fallback rebuilds index by embedding keywords online in request path (`app/services/responder/rag/keyword_filter.py:456-476#_ensure_index`).
  - **P1 deployment coupling**: API/bot/workers are hard-coupled to precompute container + shared `app_data` volume for RAG availability (`docker-compose.yml:154-159`, `docker-compose.yml:206-214`).
  - **P1 invariant gap (dimension)**: no explicit repository-level invariant for 3072; dimension is inferred from loaded arrays/files (`app/services/responder/rag/knowledge_proc.py:85-89#_load_precomputed`, `app/tasks/kb.py:322-323#_rebuild_for_api_key_async`).
  - **P2 dead-end operational complexity**: orphan-directory GC and file invalidation logic exist only to maintain local vector artifacts (`app/tasks/kb.py:527-557#clear_for_api_key`, `app/tasks/kb.py:560-620#gc_orphan_api_key_dirs`).
- Constraints:
  - Existing public request flow must be preserved (same `/conversation` async queue + worker path) (`app/api/conversation.py:937-955`, `app/tasks/api_worker.py:705-708`).
  - Current repository-native validation is `bash scripts/check.sh` (`docs/ci_checks.md:1-12`, `scripts/check.sh:20-24`).

## 1. North Star
- UX outcomes:
  - No RAG cold-start dependency on local embedding files/sidecar precompute container; request behavior remains deterministic after deploy restart.
  - Proxy metric: 0 startup blockers related to missing `knowledge_embedded_*.npz|json` across services.
- Domain outcomes:
  - Single source of truth for vectors in PostgreSQL (global + per-owner + tags), while preserving current relevance + MMR behavior contracts.
  - Enforced invariant: all persisted/query vectors are exactly 3072 dimensions.
- Engineering outcomes:
  - Reduced operational coupling (remove file GC/precompute orchestration).
  - Lower regression risk via migration + contract tests for DB-backed retrieval path.

## 2. Roadmap (incremental)

### Phase 1 (Stabilize Core) - up to 10 highest-impact tasks (prioritize P0/P1)
- Goal
  - Move vector source-of-truth from filesystem to PostgreSQL/pgvector with strict 3072 invariant, without changing external API flow.
- Scope (what we touch / what we don’t)
  - Touch: alembic schema, RAG storage/retrieval modules, KB rebuild task.
  - Don’t touch: API request contract (`/conversation` payload/response), billing/idempotency flow.
- Deliverables (concrete changes)
  - pgvector tables + indexes for content/tag vectors.
  - DB-backed read path in `knowledge_proc`, `api_kb_proc`, `keyword_filter`.
  - KB rebuild writes vectors to DB transactionally and marks ready only after successful write.
- Dependencies
  - PostgreSQL extension availability and migration rollout.
- Risk & Rollback strategy (if migration/contract changes are required)
  - Additive schema first; dual-read fallback gate during rollout; rollback by switching read flag back to old source until cutover completion.
- Validation (how to verify: tests/linter/commands from the repo)
  - `bash scripts/check.sh`.

### Phase 2 (UX & Domain Consolidation) - up to 10 tasks
- Goal
  - Eliminate file-based operational coupling and make RAG readiness predictable across all services.
- Scope (what we touch / what we don’t)
  - Touch: docker-compose orchestration, KB GC/invalidations tied to files, observability fields for DB retrieval.
  - Don’t touch: user-facing conversation semantics and queue topology.
- Deliverables (concrete changes)
  - Remove mandatory `precompute-embeddings` dependency chain for runtime services.
  - Replace file-GC maintenance with DB cleanup lifecycle.
- Dependencies
  - Phase 1 DB path fully available.
- Risk & Rollback strategy (if migration/contract changes are required)
  - Keep compatibility switch until post-cutover smoke passes.
- Validation (how to verify: tests/linter/commands from the repo)
  - `bash scripts/check.sh`.

### Phase 3 (Scale & Maintainability)- up to 10 tasks (only if it truly blocks progress)
- Goal
  - Lock in performance/maintainability for larger KB sizes and frequent updates.
- Scope (what we touch / what we don’t)
  - Touch: query plans/index tuning, regression tests for load-sensitive paths.
  - Don’t touch: product behavior and API contract.
- Deliverables (concrete changes)
  - pgvector index/plan tuning and deterministic test coverage for top-K relevance equivalence.
- Dependencies
  - Phase 1/2 complete and stable.
- Risk & Rollback strategy (if migration/contract changes are required)
  - Index changes are reversible; keep prior index until verification complete.
- Validation (how to verify: tests/linter/commands from the repo)
  - `bash scripts/check.sh`.

## 3. Task Specs (atomic, single-strategy)

### EVO-001
- ID: EVO-001
- Priority: P0
- Theme: Platform
- Problem:
  - Vector data is not persisted in DB and cannot use PostgreSQL vector search.
- Evidence:
  - `api_key_knowledge` stores metadata only, no vector columns/tables (`app/core/models.py:161-183#ApiKeyKnowledge`).
  - Base migration creates no vector entities (`alembic/versions/0001_initial_schema.py:61-77#upgrade`).
- Root Cause
  - Initial design stores embeddings as local artifacts (JSON/NPZ) outside relational schema.
- Impact
  - Impossible to switch to gpvector/pgvector as source of truth without schema layer; cross-service consistency depends on filesystem.
- Fix (single solution)
  - Add Alembic migration introducing pgvector extension and normalized vector tables for:
    - global knowledge chunks,
    - owner-scoped chunks,
    - owner/system tag vectors,
    all with `vector(3072)` and required metadata keys.
- Steps
  1. Add extension migration (`CREATE EXTENSION IF NOT EXISTS vector`).
  2. Create vector tables with FK/version ownership semantics.
  3. Add ANN indexes matching retrieval predicates.
- Acceptance Criteria (verifiable)
  - Schema contains vector tables with dimension 3072 and indexes.
  - Migration is reversible via downgrade.
- Validation Commands (if visible in the project)
  - `bash scripts/check.sh`
- Migration/Rollback (if needed)
  - Rollback via Alembic downgrade to pre-vector schema.
- Сделать:
  - Ввести pgvector-схему и индексы под текущие RAG-сценарии.
- Файлы:
  - `alembic/versions/*`, `app/core/models.py`.
- DoD:
  - Таблицы/индексы есть, миграция применима и обратима.
- Проверка:
  - `bash scripts/check.sh`.

### EVO-002
- ID: EVO-002
- Priority: P0
- Theme: Reliability
- Problem:
  - KB rebuild marks readiness in DB but vectors are produced as files, creating split-brain state.
- Evidence:
  - Rebuild writes JSON/NPZ files (`app/tasks/kb.py:269-342#_rebuild_for_api_key_async`).
  - Status switches to `ready` in DB afterwards (`app/tasks/kb.py:476-482#_rebuild_for_api_key_async`).
  - Reader first checks DB ready, then loads NPZ from disk (`app/services/responder/rag/api_kb_proc.py:144-154#_ensure_state`).
- Root Cause
  - Persistence and readiness transaction are separated across DB and filesystem.
- Impact
  - DB can report ready while retrieval returns empty due to missing/corrupted local files.
- Fix (single solution)
  - Make KB rebuild write vectors directly into pgvector tables inside one DB transaction and set `status=ready` only after successful commit.
- Steps
  1. Replace file-writing blocks with DB insert/upsert blocks.
  2. Keep `status=building/failed/ready` transitions around DB transaction.
  3. Remove NPZ/JSON success as readiness criteria.
- Acceptance Criteria (verifiable)
  - For a rebuilt KB, rows exist in vector tables and `api_key_knowledge.status='ready'` atomically.
- Validation Commands (if visible in the project)
  - `bash scripts/check.sh`
- Migration/Rollback (if needed)
  - Feature-flag read path rollback while keeping additive writes.
- Сделать:
  - Перевести rebuild на транзакционную запись в PostgreSQL.
- Файлы:
  - `app/tasks/kb.py`, `app/core/models.py`.
- DoD:
  - Нет обязательной записи NPZ/JSON для готовности KB.
- Проверка:
  - `bash scripts/check.sh`.

### EVO-003
- ID: EVO-003
- Priority: P0
- Theme: Domain
- Problem:
  - Runtime retrieval depends on local NPZ/JSON loaders instead of database vectors.
- Evidence:
  - Global loader reads NPZ/JSON files from `EMBED_DIR` (`app/services/responder/rag/knowledge_proc.py:24-31#_npz_path`, `app/services/responder/rag/knowledge_proc.py:201-209#_init_kb`).
  - Owner loader reads owner NPZ (`app/services/responder/rag/api_kb_proc.py:31-39#_load_state_from_npz`).
  - Tag retrieval reads TAGS NPZ (`app/services/responder/rag/keyword_filter.py:413-420#_load_precomputed_tags_index`).
- Root Cause
  - Retrieval abstractions are bound to filesystem snapshots.
- Impact
  - Cannot achieve gpvector-backed hybrid RAG while preserving current flows.
- Fix (single solution)
  - Refactor `knowledge_proc`, `api_kb_proc`, `keyword_filter` retrieval to execute pgvector similarity queries (+ existing MMR post-selection) directly from PostgreSQL.
- Steps
  1. Add DB query helpers for content/tag vector candidates.
  2. Feed candidate sets into existing MMR logic to preserve selection behavior.
  3. Remove file-read initialization from runtime path.
- Acceptance Criteria (verifiable)
  - Retrieval returns non-empty hits from DB without requiring local embedding files.
- Validation Commands (if visible in the project)
  - `bash scripts/check.sh`
- Migration/Rollback (if needed)
  - Keep compatibility flag for temporary fallback during rollout.
- Сделать:
  - Перенести чтение кандидатов RAG из файлов в SQL (pgvector).
- Файлы:
  - `app/services/responder/rag/knowledge_proc.py`, `app/services/responder/rag/api_kb_proc.py`, `app/services/responder/rag/keyword_filter.py`.
- DoD:
  - В прод-пути нет обязательного чтения `knowledge_embedded_*.npz/json`.
- Проверка:
  - `bash scripts/check.sh`.

### EVO-004
- ID: EVO-004
- Priority: P1
- Theme: Reliability
- Problem:
  - No hard 3072-dimension invariant across ingest and query.
- Evidence:
  - Dimension is inferred from first row (`app/services/responder/rag/knowledge_proc.py:85-89#_load_precomputed`).
  - Rebuild metadata stores `dim` dynamically from produced arrays (`app/tasks/kb.py:322-323#_rebuild_for_api_key_async`, `app/tasks/kb.py:435-436#_rebuild_for_api_key_async`).
- Root Cause
  - Existing flow accepts variable dimensions based on source artifacts/model output.
- Impact
  - Mixed dimensions can silently degrade retrieval quality or fail at query time.
- Fix (single solution)
  - Introduce explicit `RAG_VECTOR_DIM=3072` invariant and enforce it at embed ingest, persistence, and query boundaries with fail-closed errors.
- Steps
  1. Add centralized dimension constant/config.
  2. Validate embedding length before write/read/query.
  3. Mark KB build failed on mismatch.
- Acceptance Criteria (verifiable)
  - Any non-3072 vector is rejected and cannot be marked ready.
- Validation Commands (if visible in the project)
  - `bash scripts/check.sh`
- Migration/Rollback (if needed)
  - Existing non-conforming records remain excluded until rebuilt.
- Сделать:
  - Зафиксировать инвариант размерности 3072 по всему контуру.
- Файлы:
  - `app/config.py`, `app/tasks/kb.py`, `app/services/responder/rag/*.py`.
- DoD:
  - Все write/read пути валидируют длину вектора = 3072.
- Проверка:
  - `bash scripts/check.sh`.

### EVO-005
- ID: EVO-005
- Priority: P1
- Theme: Performance
- Problem:
  - Request path may trigger online keyword embedding/index build fallback.
- Evidence:
  - If precomputed index missing, `_ensure_index` loads JSON and calls `_embed_texts` to build vectors in-memory (`app/services/responder/rag/keyword_filter.py:456-476#_ensure_index`).
- Root Cause
  - Index availability is optional and fallback does heavy work in hot path.
- Impact
  - Increased first-hit latency and unpredictable response times.
- Fix (single solution)
  - Remove runtime fallback build and require DB-backed tag vectors; on absence return fail-closed empty hits with explicit log.
- Steps
  1. Delete JSON→runtime-embed fallback branch.
  2. Keep explicit diagnostics when tag index absent.
  3. Ensure rebuild pipeline always writes tag vectors to DB.
- Acceptance Criteria (verifiable)
  - `find_tag_hits` no longer performs runtime keyword embedding/index construction.
- Validation Commands (if visible in the project)
  - `bash scripts/check.sh`
- Migration/Rollback (if needed)
  - Temporarily gate by feature flag until all tenants rebuilt.
- Сделать:
  - Убрать runtime rebuild индекса тегов из hot path.
- Файлы:
  - `app/services/responder/rag/keyword_filter.py`, `app/tasks/kb.py`.
- DoD:
  - На запросах нет тяжелого построения индекса/эмбеддинга тегов.
- Проверка:
  - `bash scripts/check.sh`.

### EVO-006
- ID: EVO-006
- Priority: P1
- Theme: Platform
- Problem:
  - Runtime services depend on precompute container and shared embedding filesystem.
- Evidence:
  - Service dependency on `precompute-embeddings` (`docker-compose.yml:154-155`, `docker-compose.yml:206-207`).
  - `precompute-embeddings` writes local JSON output (`docker-compose.yml:107-122`).
  - Runtime mounts shared `app_data` for embedding files (`docker-compose.yml:159`, `docker-compose.yml:213`).
- Root Cause
  - Deployment bootstrap is designed around local artifact generation.
- Impact
  - Extra deploy step, tighter coupling, and more failure modes unrelated to DB readiness.
- Fix (single solution)
  - Update deployment to initialize vectors via DB migrations/jobs and remove mandatory filesystem embedding artifacts from runtime startup dependencies.
- Steps
  1. Remove hard dependency chain on precompute service.
  2. Keep only DB migration dependency for runtime services.
  3. Document DB-backed RAG bootstrap in deploy docs.
- Acceptance Criteria (verifiable)
  - bot/api/worker start without `precompute-embeddings` and without local RAG files.
- Validation Commands (if visible in the project)
  - `bash scripts/check.sh`
- Migration/Rollback (if needed)
  - Keep precompute service as temporary optional fallback until final cutover.
- Сделать:
  - Упростить запуск: RAG поднимается от БД, не от файлового precompute.
- Файлы:
  - `docker-compose.yml`, `docs/deploy.md`.
- DoD:
  - Удалена обязательная зависимость рантайма от `precompute-embeddings`.
- Проверка:
  - `bash scripts/check.sh`.

### EVO-007
- ID: EVO-007
- Priority: P1
- Theme: Reliability
- Problem:
  - Current tests are heavily file-based for RAG and do not lock DB-vector contracts.
- Evidence:
  - Existing RAG tests patch file-based modules and NPZ behavior (`tests/test_knowledge_proc_concurrency.py:13-30`, `tests/test_api_kb_proc_fail_closed.py:116-124`).
  - CI uses repository-native `pytest` path (`scripts/check.sh:20-21`, `docs/ci_checks.md:5-12`).
- Root Cause
  - Test suite evolved around local artifacts instead of DB vector layer.
- Impact
  - Migration to pgvector risks regressions in relevance gating and owner scoping.
- Fix (single solution)
  - Replace/add tests that assert DB-backed retrieval contracts (scope isolation, top-k/MMR ordering, fail-closed on missing ready KB, 3072 validation).
- Steps
  1. Add unit/integration tests for vector SQL readers and rebuild writes.
  2. Update legacy file-based tests to new DB contract where applicable.
  3. Keep existing request-flow tests unchanged.
- Acceptance Criteria (verifiable)
  - Test suite covers DB-backed global/owner/tag retrieval and passes in CI script.
- Validation Commands (if visible in the project)
  - `bash scripts/check.sh`
- Migration/Rollback (if needed)
  - Keep compatibility tests only during transition window; remove after full cutover.
- Сделать:
  - Зафиксировать контракт DB-RAG тестами до удаления файлового пути.
- Файлы:
  - `tests/test_*rag*`, `tests/test_api_kb_proc_*`, `tests/test_knowledge_proc_*`.
- DoD:
  - Критичные сценарии DB-RAG покрыты и проходят в CI-проверках.
- Проверка:
  - `bash scripts/check.sh`.

## 4. Explicit Non-Goals
- Do not redesign conversation API contract or queue semantics (`app/api/conversation.py:937-955`, `app/tasks/api_worker.py:705-708`).
- Do not change billing, idempotency, or moderation flows unrelated to vector storage (`app/api/conversation.py:979-1014`).
- Do not introduce multi-backend vector stores; only PostgreSQL pgvector path is in scope.
- Do not optimize unrelated subsystems (persona engine, payments, battle logic).
