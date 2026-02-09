# Evolution Plan

## 0. Baseline (from audit)
- Architecture map: Telegram bot (aiogram) handlers in `app/bot`, public API (FastAPI) in `app/api`, async workers/queues in `app/tasks`, domain services and responders in `app/services`, shared persistence/Redis utilities in `app/core`, personas/emo engine in `app/emo_engine`. Main entrypoint orchestrates bot + API + scheduler in `main.py`.
- Critical flows:
  - Telegram private message → access/billing guard → debouncer → Redis queue → worker response (bot UX).
  - Telegram payments (buy/gift) → invoice → payment receipt → balance update → UI refresh.
  - Public API `/api/v1/conversation` → API key auth → billing decrement → Redis queue → worker response.
  - Persona preferences update for API users (per key or per user) → cache refresh.
  - Scheduler tasks (periodic pings, posts, analytics).
- Current pain points:
  - Payments: if DB write fails during successful payment processing, user may be charged without balance update; no durable retry/outbox. Evidence: `app/bot/handlers/payments.py:on_payment_success`.
  - Bot billing: request is consumed before enqueueing response; enqueue failures lose both response and credits (no refund path). Evidence: `app/bot/handlers/private.py:_ensure_access_and_increment` + `app/bot/utils/debouncer.py:_enqueue`.
  - API persona preferences update is read-modify-write without row locking/atomic merge → lost updates under concurrent calls. Evidence: `app/api/conversation.py` (select + merge + update for `ApiKey.persona_prefs`/`User.persona_prefs`).
  - API rate limiter fails open on Redis errors → unlimited usage and cost risk under infra faults. Evidence: `app/api/conversation.py:_check_rate_limit`.
  - Welcome/join handling duplicates state updates in two handlers → divergence risk and inconsistent onboarding UX. Evidence: `app/bot/handlers/welcome.py:on_new_members` + `on_user_join_via_chat_member`.
  - Test coverage gap for critical billing/API flows; current tests focus on scheduler/persona/pipeline/audio. Evidence: `tests/` contents.
- Constraints:
  - Redis queue + PostgreSQL are core dependencies for bot/API/worker flows (`app/core/memory.py`, `app/core/db.py`).
  - Billing depends on `User`/`ApiKey` models and JSONB preferences (`app/core/models.py`).
  - Existing settings/flags drive UX (shop/buttons/welcome) and API behavior (`app/config.py`).

## 1. North Star (12–16 недель)
- UX outcomes:
  - Critical flows (payments, shop access, API conversation) have predictable outcomes: no lost credits, clear retry paths, and consistent pending states.
  - Reduce friction in paid flows by ensuring retries do not double-charge and by exposing deterministic states to the user/client.
- Domain outcomes:
  - Billing and persona preferences are updated atomically with no lost updates; explicit invariants around credit consumption/refunds.
- Engineering outcomes:
  - Regression risk reduced via targeted tests for billing + API request lifecycle.
  - Lower incident risk via bounded rate-limiting behavior under Redis outages.

## 2. Roadmap (инкрементально)

### Phase 1 (Stabilize Core)
- Goal: устранить P0/P1 ошибки в платежах, биллинге и API-критических путях.
- Scope: payment processing, billing guard, API request lifecycle, rate limiting.
- Deliverables:
  - EVO-001, EVO-002, EVO-003, EVO-004, EVO-005.
- Dependencies: Redis + Postgres доступность; без изменения внешних контрактов API/бота.
- Risk & Rollback strategy: миграции/feature flags для новых таблиц/инструментов; быстрый откат через флаги.
- Validation: существующий `scripts/check.sh` + новые целевые тесты.

### Phase 2 (UX & Domain Consolidation)
- Goal: унифицировать повторяющуюся бизнес-логику, стабилизировать UX в онбординге/магазине.
- Scope: welcome/onboarding, shop UI state, bot UX paths.
- Deliverables:
  - EVO-006, EVO-007, EVO-008, EVO-009, EVO-010.
- Dependencies: результаты Phase 1.
- Risk & Rollback strategy: UI-изменения за флагами; пошаговое включение.
- Validation: `scripts/check.sh` + UX smoke сценарии.

### Phase 3 (Scale & Maintainability)
- Goal: улучшить наблюдаемость и поддерживаемость критических доменных инвариантов.
- Scope: monitoring, metrics, performance guardrails.
- Deliverables:
  - EVO-011, EVO-012, EVO-013, EVO-014, EVO-015 (если после Phase 2 остаются блокеры).
- Dependencies: устойчивые данные и тесты после Phase 1–2.
- Risk & Rollback strategy: метрики/алерты без влияния на внешний контракт.
- Validation: `scripts/check.sh` + выборочные нагрузочные проверки (если предусмотрены в репо).

## 3. Task Specs (атомарно, по одной стратегии)

### EVO-001
- Priority: P0
- Theme: Reliability | Domain
- Problem: Успешные платежи могут не попасть в БД/баланс при сбое БД; пользователь уже заплатил.
- Evidence: `app/bot/handlers/payments.py:on_payment_success` (DB insert + balance update внутри try; при исключении только лог/сообщение без retry).
- Root Cause: нет устойчивого механизма фиксации платежа и повторного применения (outbox/retry).
- Impact: потеря доверия и ручные возвраты; высокий бизнес-риск.
- Fix (single solution): внедрить payment-outbox: записывать «pending» платеж в БД до бизнес-логики, обрабатывать идемпотентно воркером/ретраем до «applied».
- Steps:
  1) Добавить таблицу outbox/статус платежа.
  2) Перенести логику начисления в фонового обработчика (идемпотентного по `telegram_payment_charge_id`).
  3) Обновить обработчик успешного платежа: только запись pending + уведомление «обработаем».
- Acceptance Criteria: оплата никогда не теряется; при DB сбое платеж доезжает после восстановления.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: миграция БД + флаг для включения outbox.
- Сделать: «Outbox для платежей + идемпотентный обработчик начислений».
- Файлы: `app/bot/handlers/payments.py`, `app/core/models.py`, `app/tasks` (новый обработчик), миграции.
- DoD: тест на идемпотентность начисления; новый статус платежа учитывается в UI/логах.
- Проверка: `scripts/check.sh` + unit тесты на outbox-обработчик.

### EVO-002
- Priority: P1
- Theme: Domain | Reliability
- Problem: В бот-потоке списание запроса происходит до успешной постановки задачи в очередь; при сбое очереди кредит теряется.
- Evidence: `app/bot/handlers/private.py:_ensure_access_and_increment` (consume_request до enqueue) + `app/bot/utils/debouncer.py:_enqueue` (нет обработки ошибок).
- Root Cause: отсутствие резервирования/rollback для биллинга и «at-least-once» подтверждения enqueue.
- Impact: пользователи теряют запросы без ответа, рост поддержки.
- Fix (single solution): внедрить «резервирование» запроса: списывать после успешного enqueue; при неуспехе — не списывать/возвращать.
- Steps:
  1) Добавить модель/таблицу резерва или использовать атомарный флаг «pending_consume».
  2) Обновить `_ensure_access_and_increment` на резерв вместо списания.
  3) Подтверждать списание в воркере после обработки/постановки.
- Acceptance Criteria: при сбое очереди запрос не списывается.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: миграция БД + флаг для режима резервирования.
- Сделать: «Резервирование запросов до enqueue + подтверждение в воркере».
- Файлы: `app/bot/handlers/private.py`, `app/bot/utils/debouncer.py`, воркер очереди.
- DoD: тесты на enqueue failure; подтверждение списания в одном месте.
- Проверка: `scripts/check.sh`.

### EVO-003
- Priority: P1
- Theme: Domain
- Problem: Персона-предпочтения в API обновляются read-modify-write без блокировки → потеря обновлений при конкурентных запросах.
- Evidence: `app/api/conversation.py` (select `persona_prefs` → `merge_prefs` → update).
- Root Cause: отсутствие атомарного JSONB merge/lock.
- Impact: неконсистентные предпочтения → непредсказуемая персона/UX.
- Fix (single solution): выполнить атомарный merge в SQL (`jsonb || ...`) или `SELECT ... FOR UPDATE`.
- Steps:
  1) Добавить SQL-merge для `persona_prefs`.
  2) Удалить промежуточное чтение в приложении.
  3) Обновить `update_cached_personas_for_owner` только после успешного update.
- Acceptance Criteria: конкурентные обновления не теряются.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Атомарное обновление persona_prefs».
- Файлы: `app/api/conversation.py`, `app/emo_engine/persona/constants/user_prefs.py`.
- DoD: тест на конкурентные обновления.
- Проверка: `scripts/check.sh`.

### EVO-004
- Priority: P1
- Theme: Reliability | Platform
- Problem: Rate limiter в API работает в fail-open режиме при ошибках Redis → неограниченные запросы.
- Evidence: `app/api/conversation.py:_check_rate_limit` (except → warning + allow).
- Root Cause: отсутствие fallback-ограничителя.
- Impact: риск взрывного трафика и затрат.
- Fix (single solution): локальный in-memory fallback limiter с минимальными лимитами при ошибке Redis.
- Steps:
  1) Ввести локальный лимитер по API key/IP с TTL.
  2) Активировать его только при исключениях Redis.
  3) Логировать fallback и метрику.
- Acceptance Criteria: при Redis outage лимиты сохраняются.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Fallback rate limiter при Redis сбое».
- Файлы: `app/api/conversation.py`.
- DoD: тест на fail-open → fallback.
- Проверка: `scripts/check.sh`.

### EVO-005
- Priority: P1
- Theme: Reliability | Domain
- Problem: API не поддерживает идемпотентность запросов (retries клиента могут удвоить списание).
- Evidence: `app/api/conversation.py` (request_id генерируется сервером, нет idempotency key).
- Root Cause: отсутствие явного ключа идемпотентности/дедупликации.
- Impact: двойное списание при сетевых retry, ухудшение UX и поддержки.
- Fix (single solution): принять Idempotency-Key заголовок и хранить результаты/статусы в Redis с TTL.
- Steps:
  1) Ввести заголовок `Idempotency-Key` и ключи в Redis.
  2) Возвращать сохранённый ответ для повторов.
  3) Документировать в API.
- Acceptance Criteria: повторный запрос с тем же ключом не списывает заново.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Idempotency-Key для API /conversation».
- Файлы: `app/api/conversation.py`, `app/core/memory.py` (при необходимости ключи).
- DoD: тест на повторный запрос с одинаковым ключом.
- Проверка: `scripts/check.sh`.

### EVO-006
- Priority: P1
- Theme: Domain | UX
- Problem: Дублирование логики welcome/онбординга в двух обработчиках усложняет изменения и приводит к расхождениям.
- Evidence: `app/bot/handlers/welcome.py:on_new_members` и `on_user_join_via_chat_member` (копия pipeline).
- Root Cause: отсутствие общего helper для join-flow.
- Impact: риск несогласованного поведения в группах.
- Fix (single solution): вынести общий pipeline в один helper и вызвать из обоих обработчиков.
- Steps:
  1) Создать helper для join-flow (redis pipeline + analytics + greet scheduling).
  2) Заменить дублирующиеся участки.
- Acceptance Criteria: единый путь логики, одинаковые side-effects.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Единый helper для group welcome».
- Файлы: `app/bot/handlers/welcome.py`.
- DoD: unit-тесты/минимальные проверки join-flow.
- Проверка: `scripts/check.sh`.

### EVO-007
- Priority: P2
- Theme: UX | Reliability
- Problem: Отсутствуют тесты критических UX/биллинг потоков (API conversation, payments).
- Evidence: `tests/` содержит тесты только для scheduler/persona/pipeline/audio.
- Root Cause: приоритет на технические тесты без e2e/flow coverage.
- Impact: регрессии в оплатах/биллинг-логике остаются незамеченными.
- Fix (single solution): добавить минимальный набор unit/integration тестов для API conversation и payment success flow.
- Steps:
  1) Тесты на billing decrement/refund и persona prefs update.
  2) Тесты на payment success (idempotent receipt).
- Acceptance Criteria: тесты покрывают ключевые ветки и проходят локально.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Минимальный тестовый пакет для биллинга/платежей/API».
- Файлы: `tests/` (новые тесты).
- DoD: как минимум 1 тест на API billing + 1 на payment success.
- Проверка: `scripts/check.sh`.

### EVO-008
- Priority: P2
- Theme: UX
- Problem: В UI shop/requests отсутствует единое сообщение о pending-состоянии при повторных действиях.
- Evidence: `app/bot/handlers/payments.py:show_pending_invoice_stub` (логика pending распределена по нескольким хендлерам).
- Root Cause: pending-state UI/flow разнесён, отсутствует единый entrypoint.
- Impact: непредсказуемое поведение при повторных нажатиях/командах.
- Fix (single solution): централизовать pending-UI в одном helper и вызывать перед любыми shop действиями.
- Steps:
  1) Создать shared helper `ensure_no_pending_or_show_stub`.
  2) Использовать в `cmd_buy`, `cmd_buy_reqs`, callbacks.
- Acceptance Criteria: consistent pending UX across entrypoints.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Единый pending UX helper для shop».
- Файлы: `app/bot/handlers/payments.py`.
- DoD: manual QA сценарии «pending invoice».
- Проверка: `scripts/check.sh`.

### EVO-009
- Priority: P2
- Theme: Performance | UX
- Problem: В API latency breakdown возвращается частично и не документирован, что усложняет клиентскую диагностику.
- Evidence: `app/api/conversation.py` (latency_breakdown optional, без гарантии полей).
- Root Cause: отсутствие согласованного контракта/документации по метрикам.
- Impact: ухудшение клиентского UX и поддержки.
- Fix (single solution): документировать контракт (какие поля гарантированы) и возвращать нули вместо отсутствующих значений.
- Steps:
  1) Обновить response model/документацию.
  2) Нормализовать поля latency_breakdown.
- Acceptance Criteria: API возвращает стабильные поля, клиенты не ломаются.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Стабилизировать contract latency_breakdown».
- Файлы: `app/api/conversation.py`.
- DoD: тест на response schema.
- Проверка: `scripts/check.sh`.

### EVO-010
- Priority: P2
- Theme: Domain | UX
- Problem: UI кнопки quick-links используют один флаг для нескольких кнопок, усложняя управляемость меню.
- Evidence: `app/bot/handlers/private.py:build_quick_links_kb` (SHOW_SHOP_BUTTON управляет двумя кнопками).
- Root Cause: нет отдельного флага для «Shop» и «Requests».
- Impact: ограниченная настройка меню, сложнее A/B для UX.
- Fix (single solution): разделить флаги/настройки для отдельных кнопок.
- Steps:
  1) Ввести отдельный flag для `menu.requests`.
  2) Обновить build_quick_links_kb.
- Acceptance Criteria: независимое управление кнопками.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: безопасно (defaults preserve behavior).
- Сделать: «Разделить флаги для Shop/Requests кнопок».
- Файлы: `app/bot/handlers/private.py`, `app/config.py`.
- DoD: ручная проверка меню.
- Проверка: `scripts/check.sh`.

### EVO-011
- Priority: P2
- Theme: Reliability
- Problem: нет явного мониторинга для очереди API (latency/timeout/queue depth).
- Evidence: `app/api/conversation.py` и `app/tasks/api_worker.py` используют Redis очередь, но без метрик.
- Root Cause: отсутствие метрик/логов с агрегированием.
- Impact: сложно обнаруживать деградацию и таймауты.
- Fix (single solution): добавить счетчики/метрики (логовые или Prometheus, если есть) по enqueue/timeout.
- Steps:
  1) Логировать queue_wait_ms + timeouts в одном формате.
  2) Добавить периодический snapshot queue length.
- Acceptance Criteria: SLA мониторится, видны таймауты.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Метрики очереди API».
- Файлы: `app/api/conversation.py`, `app/tasks/api_worker.py`.
- DoD: метрики доступны в логах/дашборде.
- Проверка: `scripts/check.sh`.

### EVO-012
- Priority: P2
- Theme: Performance
- Problem: JSON сериализация в hot-path очереди без ограничений на payload size.
- Evidence: `app/api/conversation.py:_send_job_and_wait` и `app/bot/utils/debouncer.py:_enqueue` (json.dumps без size guard).
- Root Cause: отсутствие лимитов на payload на уровне очереди.
- Impact: риск больших payload → лаги/Redis memory pressure.
- Fix (single solution): добавить верхний предел payload size и отказ с явной ошибкой.
- Steps:
  1) Ввести лимит на размер job payload.
  2) Возвращать 413/400 при превышении.
- Acceptance Criteria: очередь не принимает чрезмерные payload.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Лимиты размера payload для очереди».
- Файлы: `app/api/conversation.py`, `app/bot/utils/debouncer.py`.
- DoD: тест на превышение лимита.
- Проверка: `scripts/check.sh`.

### EVO-013
- Priority: P2
- Theme: Platform
- Problem: большой объём конфиг-флагов без группировки затрудняет поддержку.
- Evidence: `app/config.py` содержит большое число UX/behavior флагов.
- Root Cause: отсутствие структурирования/документации.
- Impact: риск неверных комбинаций флагов.
- Fix (single solution): сгруппировать настройки в секции/документировать default behavior.
- Steps:
  1) Добавить README/док для ключевых флагов.
  2) Указать рекомендуемые комбинации.
- Acceptance Criteria: понятный набор флагов и их эффекты.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Документация и группировка UX флагов».
- Файлы: `app/config.py`, docs (если есть).
- DoD: документ обновлён.
- Проверка: `scripts/check.sh`.

### EVO-014
- Priority: P2
- Theme: Reliability
- Problem: нет единого контракта/схемы для сообщений очереди (bot/API используют разные структуры без формальной проверки).
- Evidence: `app/bot/utils/debouncer.py` формирует payload вручную, `app/tasks/queue_worker.py` ожидает поля; API воркер получает другие поля.
- Root Cause: отсутствие схемы/валидатора.
- Impact: риск silent failures при изменениях.
- Fix (single solution): ввести Pydantic/TypedDict схемы для payload и валидировать при enqueue.
- Steps:
  1) Создать схему payload для bot и API.
  2) Валидировать перед enqueue.
- Acceptance Criteria: некорректные payload отклоняются с логированием.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Схема и валидация payload очереди».
- Файлы: `app/bot/utils/debouncer.py`, `app/tasks/queue_worker.py`, `app/tasks/api_worker.py`.
- DoD: тесты на валидацию схемы.
- Проверка: `scripts/check.sh`.

### EVO-015
- Priority: P2
- Theme: Performance | Reliability
- Problem: нет тестов на регрессии debounce/queue batching.
- Evidence: отсутствуют тесты в `tests/` для debouncer.
- Root Cause: отсутствие unit coverage для debounce logic.
- Impact: риск регрессий в очереди/latency.
- Fix (single solution): добавить unit тесты для debounce modes (single/merge/human).
- Steps:
  1) Добавить тесты на batching и size limits.
  2) Проверить enqueue order.
- Acceptance Criteria: debounce behavior защищён тестами.
- Validation Commands: `scripts/check.sh`
- Migration/Rollback: нет.
- Сделать: «Unit тесты debouncer».
- Файлы: `tests/` + `app/bot/utils/debouncer.py`.
- DoD: тесты покрывают три режима.
- Проверка: `scripts/check.sh`.

## 4. Explicit Non-Goals
- Переписывание архитектуры или смена фреймворков.
- Массовые рефакторинги без влияния на UX/доменные инварианты.
- Изменение публичных контрактов API без версии/миграции.
