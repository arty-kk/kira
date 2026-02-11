# Evolution Plan

## 0. Baseline (from audit)
- Architecture map: Монолитный Python-сервис с двумя runtime-входами: Telegram webhook-бот (`main.py` → `app.bot.components.webhook.start_bot`) и HTTP API (`main.py` → `app.api.app.create_app` + `app.api.conversation`). Доменная логика сосредоточена в `app/api`, `app/tasks`, `app/services`, `app/emo_engine`; состояние хранится в PostgreSQL (`app/core/models.py`) и Redis (`app/core/memory.py`).
- Critical flows:
  1. API `/api/v1/conversation`: auth API key → rate limit → idempotency → enqueue/worker/response (`app/api/conversation.py`, `app/tasks/api_worker.py`).
  2. Telegram webhook ingress: `set_webhook` + приём update + dedup + dispatch (`app/bot/components/webhook.py`).
  3. Очереди и фоновые задачи: Celery tasks в `app/tasks/*` (payments/refunds/media/moderation/scheduler).
  4. Платежи: Telegram payment success → outbox → processing/requeue (`app/bot/handlers/payments.py`, `app/tasks/payments.py`).
  5. Возвраты: refund outbox + ретраи (`app/tasks/refunds.py`, `app/api/conversation.py`).
  6. Persona engine: state/memory/snapshot/recompute (`app/emo_engine/persona/*`, `app/emo_engine/registry.py`).
  7. API knowledge-base ingestion/retrieval (`app/services/responder/rag/*`, `app/tasks/kb.py`).
  8. Moderation/media preprocess в group/API потоках (`app/tasks/media.py`, `app/tasks/moderation.py`, `app/bot/handlers/group.py`).
- Current pain points:
  - [P0] Миграция `0007_refund_outbox_billing_tier_check` повторно создаёт `ck_refund_outbox_billing_tier`, уже созданный в базовой миграции.
    - Evidence: `alembic/versions/0001_initial_schema.py` (создание `ck_refund_outbox_billing_tier`) + `alembic/versions/0007_refund_outbox_billing_tier_check.py` (повторный `op.create_check_constraint`).
    - Root Cause: duplicate DDL в цепочке миграций.
    - Impact: падение `alembic upgrade` и риск частично применённых релизов.
  - [P0] В webhook dedup используется `GET` + `SET` без атомарности; при конкуренции одинаковый `update_id` может быть обработан несколько раз.
    - Evidence: `app/bot/components/webhook.py` (`get` → `set` → dispatch).
    - Root Cause: TOCTOU race вместо атомарной операции Redis.
    - Impact: дублированные side-effects (повторные ответы/действия).
  - [P1] Fallback rate limiter API хранится в in-process `OrderedDict`; при multi-worker/process режиме лимиты рассинхронизируются.
    - Evidence: `_fallback_rl_state` и `_fallback_rate_limit` в `app/api/conversation.py`; fallback включается на Redis exception.
    - Root Cause: process-local состояние для межпроцессного контракта.
    - Impact: state desync и непредсказуемый rate-limit при отказе Redis.
  - [P1] Обработчик unhandled asyncio exception теряет traceback (`logging.exception` вызывается вне `except`).
    - Evidence: `_loop_ex_handler` в `main.py`.
    - Root Cause: некорректный способ логирования exception object.
    - Impact: error-handling gap и ухудшение диагностики инцидентов.
  - [P2] Дублирование TLS bootstrapping-логики в API и webhook.
    - Evidence: `main.py:start_api_server` и `app/bot/components/webhook.py:start_bot`.
    - Root Cause: copy-paste одного и того же policy-кода.
    - Impact: leaky abstraction и риск рассинхрона поведения при изменениях.
  - [P2] Нет единого test entrypoint: `pytest -q` падает на import-collection, тогда как `scripts/check.sh` запускает `unittest`.
    - Evidence: команда `pytest -q` (ошибки `ModuleNotFoundError: app`) и `scripts/check.sh`.
    - Root Cause: несогласованный раннер тестов/окружение запуска.
    - Impact: misleading quality gate и риск пропуска регрессий.
- Constraints: heavy env dependency (много ENV), внешние интеграции (Telegram/OpenAI/Redis/Postgres/Celery), асинхронная конкурентность (bot/API/workers), нежелательны контрактные изменения API.

## 1. North Star
- UX outcomes:
  - Ровно-однократная обработка webhook update в пределах dedup-окна (proxy-метрика: 0 duplicate dispatch для одинакового `update_id`).
  - Предсказуемые ответы API при деградации Redis в rate-limit (proxy: единый код/семантика ответа в одинаковых условиях).
- Domain outcomes:
  - Миграции применяются повторяемо на clean и текущей схеме.
  - Политики дедупликации и лимитов имеют единый источник истины, без process-local расхождений в критическом потоке.
- Engineering outcomes:
  - Один канонический test gate для локального и CI запуска.
  - Диагностируемые аварии event loop (traceback не теряется).

## 2. Roadmap (инкрементально)

### Phase 1 (Stabilize Core)
- Goal: устранить P0/P1 дефекты, влияющие на корректность и консистентность.
- Scope (что затрагиваем / что не трогаем):
  - Затрагиваем миграции, webhook dedup, API rate-limit degradation, loop-level error logging.
  - Не трогаем продуктовую логику persona/контент-генерации.
- Deliverables (конкретные изменения):
  - EVO-001, EVO-002, EVO-003, EVO-004.
- Dependencies:
  - Доступ к тестовому Postgres/Redis для smoke-проверок.
- Risk & Rollback strategy (если требуется миграция/контрактные изменения):
  - Для миграций: snapshot перед применением и rollback через `alembic downgrade -1`.
  - Для runtime-правок: поэтапный rollout с проверкой логов/метрик duplicate и 429.
- Validation (как проверить: тесты/линтер/команды из репо):
  - `bash scripts/check.sh`
  - `pytest -q`
  - `alembic upgrade head`
  - `alembic downgrade -1 && alembic upgrade head`

### Phase 2 (UX & Domain Consolidation)
- Goal: закрепить доменные границы и убрать рассыпанную инфраструктурную логику.
- Scope (что затрагиваем / что не трогаем):
  - Затрагиваем TLS policy consolidation и тестовый контракт запуска.
  - Не меняем публичные payload/поля API.
- Deliverables (конкретные изменения):
  - EVO-005, EVO-006.
- Dependencies:
  - Завершение Phase 1.
- Risk & Rollback strategy (если требуется миграция/контрактные изменения):
  - Централизация TLS выносится без изменения env-контрактов; rollback — возврат к старым вызовам.
- Validation (как проверить: тесты/линтер/команды из репо):
  - `bash scripts/check.sh`
  - `pytest -q tests/test_webhook_start_bot.py tests/test_main_start_api_server.py`

### Phase 3 (Scale & Maintainability)
- Goal: снизить риск повторного появления уже найденных дефектов.
- Scope (что затрагиваем / что не трогаем):
  - Затрагиваем только тесты и guard-проверки на выявленные дефекты.
  - Не добавляем новый продуктовый функционал.
- Deliverables (конкретные изменения):
  - EVO-007.
- Dependencies:
  - Завершение Phase 1–2.
- Risk & Rollback strategy (если требуется миграция/контрактные изменения):
  - Без контрактных изменений; rollback = удаление guard-теста.
- Validation (как проверить: тесты/линтер/команды из репо):
  - `bash scripts/check.sh`
  - `pytest -q`

## 3. Task Specs (атомарно, по одной стратегии)

- ID: EVO-001
- Priority: P0
- Theme: Reliability
- Problem: Alembic upgrade может падать из-за повторного создания `ck_refund_outbox_billing_tier`.
- Evidence: `alembic/versions/0001_initial_schema.py`, `alembic/versions/0007_refund_outbox_billing_tier_check.py`.
- Root Cause: duplicate DDL в истории миграций.
- Impact: блокирующие ошибки деплоя и риск partial migration.
- Fix (single solution): сделать `0007` идемпотентной через проверку существования constraint перед созданием.
- Steps:
  1. Обновить `0007` на guarded create.
  2. Добавить тестовый прогон upgrade/downgrade узла.
- Acceptance Criteria (проверяемо): `alembic upgrade head` и downgrade/upgrade проходят на clean DB без duplicate-constraint error.
- Validation Commands (если видны в проекте): `alembic upgrade head`; `alembic downgrade -1 && alembic upgrade head`.
- Migration/Rollback (если нужно): rollback через `alembic downgrade -1`.
- Сделать: устранить duplicate DDL в миграции `0007`.
- Файлы: `alembic/versions/0007_refund_outbox_billing_tier_check.py`, тесты миграций.
- DoD: миграция повторяемо применяется на clean и существующей схеме.
- Проверка: Alembic upgrade/downgrade команды.

- ID: EVO-002
- Priority: P0
- Theme: Domain
- Problem: dedup webhook update неатомарен, возможна двойная обработка.
- Evidence: `app/bot/components/webhook.py` (`GET` + `SET`).
- Root Cause: TOCTOU race.
- Impact: повторные побочные эффекты в bot-потоке.
- Fix (single solution): заменить на атомарный Redis `SET key value NX EX` и обрабатывать update только при успешной установке.
- Steps:
  1. Вынести dedup helper.
  2. Переписать условие обработки update на atomic result.
  3. Добавить race-тест на duplicate `update_id`.
- Acceptance Criteria (проверяемо): повторный `update_id` не попадает в dispatcher при конкурентных вызовах.
- Validation Commands (если видны в проекте): `bash scripts/check.sh`; `pytest -q tests/test_webhook_start_bot.py`.
- Migration/Rollback (если нужно): не требуется.
- Сделать: сделать атомарную дедупликацию Telegram updates.
- Файлы: `app/bot/components/webhook.py`, `tests/test_webhook_start_bot.py`.
- DoD: duplicate update не вызывает второй `feed_update`.
- Проверка: профильный тест webhook + общий check.

- ID: EVO-003
- Priority: P1
- Theme: Reliability
- Problem: fallback rate-limit хранится в process-local памяти и рассинхронизируется между воркерами.
- Evidence: `_fallback_rl_state`/`_fallback_rate_limit` в `app/api/conversation.py`.
- Root Cause: локальный limiter для межпроцессного сценария.
- Impact: state desync и нестабильный rate-limit в деградации Redis.
- Fix (single solution): удалить process-local fallback и применять fail-closed ответ при недоступности Redis после ограниченного retry.
- Steps:
  1. Реализовать bounded retry Redis-лимитера.
  2. Вернуть единый error code при недоступности лимитера.
  3. Обновить деградационные тесты.
- Acceptance Criteria (проверяемо): поведение rate-limit одинаково независимо от процесса.
- Validation Commands (если видны в проекте): `bash scripts/check.sh`; `pytest -q tests/test_rate_limit_ip.py`.
- Migration/Rollback (если нужно): не требуется.
- Сделать: убрать process-local fallback limiter из API.
- Файлы: `app/api/conversation.py`, `tests/test_rate_limit_ip.py`.
- DoD: нет in-process лимитера для fallback пути.
- Проверка: профильные rate-limit тесты.

- ID: EVO-004
- Priority: P1
- Theme: Reliability
- Problem: unhandled asyncio exceptions логируются без корректного traceback.
- Evidence: `_loop_ex_handler` в `main.py`.
- Root Cause: `logging.exception` используется вне `except`.
- Impact: ухудшенная диагностика production-инцидентов.
- Fix (single solution): логировать с `exc_info` из контекста loop exception handler.
- Steps:
  1. Исправить `_loop_ex_handler`.
  2. Добавить тест на наличие traceback в логе.
- Acceptance Criteria (проверяемо): traceback присутствует в loop-level error log.
- Validation Commands (если видны в проекте): `bash scripts/check.sh`.
- Migration/Rollback (если нужно): не требуется.
- Сделать: восстановить полные traceback для loop-level ошибок.
- Файлы: `main.py`, тесты логирования.
- DoD: лог содержит исключение и stack trace.
- Проверка: unit/smoke тест обработки loop exception.

- ID: EVO-005
- Priority: P2
- Theme: Platform
- Problem: TLS bootstrap-логика продублирована между API и webhook.
- Evidence: `main.py:start_api_server`, `app/bot/components/webhook.py:start_bot`.
- Root Cause: leaky abstraction / copy-paste policy-кода.
- Impact: риск расхождения поведения в одинаковой конфигурации.
- Fix (single solution): вынести общую TLS policy-функцию и использовать её в обоих entrypoint.
- Steps:
  1. Создать общий helper в `app/core`.
  2. Подключить helper из API и webhook startup.
  3. Добавить unit tests policy.
- Acceptance Criteria (проверяемо): API и webhook дают одинаковую реакцию на одинаковые TLS env-комбинации.
- Validation Commands (если видны в проекте): `bash scripts/check.sh`; `pytest -q tests/test_main_start_api_server.py tests/test_webhook_start_bot.py`.
- Migration/Rollback (если нужно): не требуется.
- Сделать: централизовать TLS policy для стартов API/webhook.
- Файлы: `app/core/*`, `main.py`, `app/bot/components/webhook.py`, профильные тесты.
- DoD: копипаст TLS-веток удалён из entrypoint.
- Проверка: startup tests API/webhook.

- ID: EVO-006
- Priority: P2
- Theme: Platform
- Problem: `pytest -q` и `scripts/check.sh` дают разные результаты, что вводит в заблуждение quality gate.
- Evidence: `scripts/check.sh`; результат запуска `pytest -q`.
- Root Cause: несогласованный test runner и импортный контекст.
- Impact: misleading проверки и риск пропуска дефектов.
- Fix (single solution): сделать единый канонический test entrypoint через `pytest` и синхронизировать `scripts/check.sh`.
- Steps:
  1. Добавить pytest-конфигурацию для текущей структуры репозитория.
  2. Перевести check script на этот runner.
  3. Убедиться, что команды дают одинаковый итог.
- Acceptance Criteria (проверяемо): `bash scripts/check.sh` использует тот же test entrypoint, что и документированный основной запуск тестов; расхождение результатов между командами отсутствует при одинаковом окружении.
- Validation Commands (если видны в проекте): `pytest -q`; `bash scripts/check.sh`.
- Migration/Rollback (если нужно): не требуется.
- Сделать: унифицировать test gate локально и в CI.
- Файлы: `scripts/check.sh`, pytest config, при необходимости тестовые настройки.
- DoD: один test entrypoint для репозитория.
- Проверка: оба command дают консистентный результат.

- ID: EVO-007
- Priority: P2
- Theme: Reliability
- Problem: найденные P0/P1 дефекты не закреплены отдельными guard-тестами и могут вернуться.
- Evidence: текущие тесты не покрывают явно duplicate-constraint migration кейс и конкурентный webhook race (по результатам аудита кода).
- Root Cause: пробелы в regression-покрытии для критических отказов.
- Impact: высокий риск повторной регрессии после будущих изменений.
- Fix (single solution): добавить точечные regression tests на EVO-001..EVO-004 сценарии.
- Steps:
  1. Добавить тест миграционного кейса duplicate-constraint.
  2. Добавить concurrency-тест webhook dedup.
  3. Добавить тест деградации rate-limit и loop exception logging.
- Acceptance Criteria (проверяемо): для каждого сценария EVO-001..EVO-004 добавлен как минимум один автотест, и они стабильно выполняются в репозиторном test gate.
- Validation Commands (если видны в проекте): `bash scripts/check.sh`; `pytest -q`.
- Migration/Rollback (если нужно): не требуется.
- Сделать: закрепить исправления критических багов regression-тестами.
- Файлы: `tests/*` по затронутым потокам.
- DoD: добавлены целевые guard-тесты, которые воспроизводимо проверяют соответствующие дефектные сценарии и предотвращают повторное появление этих регрессий.
- Проверка: полный прогон тестового gate.

## 4. Explicit Non-Goals
- Не менять продуктовую функциональность persona/генерации ответов без отдельного фактического дефекта в whitelist-категориях.
- Не делать массовые рефакторинги кода и форматирования, не влияющие на корректность/синхронизацию состояний/hot-path.
- Не менять публичные поля/формат API без необходимости для исправления найденных дефектов.
- Не менять зависимости/lock-файлы без прямой необходимости для задач EVO.
