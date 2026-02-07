# Evolution plan: Synchatica → максимальная прокачка проекта (исполняемо Codex)

Цель: быстро и безопасно прокачать качество, наблюдаемость и управляемость «движка персон» через цикл:
**propose → verify (sandbox) → evaluate (metrics) → select → apply → rollback**.

## Как использовать этот файл с Codex
- Каждая задача имеет ID вида `EVO-###`.
- Запрос к Codex: “Выполни задачу EVO-0XX”.
- Каждая задача содержит: что сделать, какие файлы трогать, критерии готовности (DoD), команды проверки.

---

## Ненарушаемые инварианты (обязательны для всех задач)
1. Внешние контракты (HTTP API, Telegram‑поведение) не менять без явного требования.【F:app/api/conversation.py†L1-L170】【F:app/bot/components/webhook.py†L1-L170】
2. Никакие изменения не принимаются без прохождения проверок (tests/compileall).【F:tests/test_scheduler.py†L1-L78】
3. Изменения должны быть минимальными и воспроизводимыми, без массовых рефакторингов.
4. Любые автоматические принятия изменений должны иметь план отката (rollback).

---

# Milestone 0 — Запускаемость и единый «контур проверки»

## EVO-001 — Добавить единый скрипт проверки
**Сделать**
- Создать `scripts/check.sh` и запускать в нём:
  1) `python -m unittest`
  2) `python -m compileall app`

**Файлы**
- `scripts/check.sh` (новый)

**DoD**
- Скрипт запускается и возвращает ненулевой код при ошибках.

**Проверка**
- `bash scripts/check.sh`

---

## EVO-002 — Документировать контуры исполнения
**Сделать**
- Добавить `docs/architecture.md` с короткими потоками:
  - API → queue → worker → responder → response
  - Telegram → webhook → dispatcher → responder

**Файлы**
- `docs/architecture.md` (новый)

**DoD**
- В документе указаны ключевые модули и ссылки на файлы: `main.py`, `app/api/conversation.py`, `app/tasks/api_worker.py`, `app/services/responder/core.py`.

**Проверка**
- Ручная проверка документа.

---

# Milestone 1 — Наблюдаемость и качество как фундамент

## EVO-003 — Сквозной request_id/persona_id в pipeline
**Сделать**
- Пробросить `request_id` и `persona_id` через API → worker → responder.
- Единый формат логов по стадиям.

**Файлы**
- `app/api/conversation.py`
- `app/tasks/api_worker.py`
- `app/services/responder/core.py`

**DoD**
- В логах есть request_id на всех стадиях.
- Метрики/логи позволяют реконструировать путь запроса.

**Проверка**
- `python -m unittest`
- `python -m compileall app`

---

## EVO-004 — Метрики стадий pipeline
**Сделать**
- Добавить измерения latency: queue_wait, LLM_call, memory_retrieval, total.

**Файлы**
- `app/services/responder/core.py`
- `app/tasks/api_worker.py`

**DoD**
- Метрики доступны и сериализуются в logs/metadata ответа.

**Проверка**
- `python -m unittest`

---

## EVO-005 — Базовый quality‑harness (7–10 сценариев)
**Сделать**
- Добавить `tests/pipeline/` с сценариями:
  - memory recall
  - style adherence
  - voice→text
  - invalid payloads
  - rate‑limit
  - long‑context
  - safety‑edge

**Файлы**
- `tests/pipeline/*` (новые)

**DoD**
- Минимум 7 тестов проходят локально.

**Проверка**
- `python -m unittest`

---

# Milestone 2 — Контракт персоны и управляемая память

## EVO-006 — PersonaContract + PersonaContext
**Сделать**
- Описать контракт `PersonaContext` и ключевые интерфейсы:
  - emotion_update
  - memory_write
  - response_synthesis

**Файлы**
- `docs/persona_contract.md` (новый)

**DoD**
- Контракт однозначный и пригоден для тестов/моков.

**Проверка**
- Ручная проверка документа.

---

## EVO-007 — Memory contract v2 (факты/доверие/источник)
**Сделать**
- Описать поля `fact/confidence/source` для памяти.
- Зафиксировать правила консолидации и дедупликации.

**Файлы**
- `docs/persona_contract.md`
- (опционально) `app/emo_engine/persona/memory.py`

**DoD**
- Правила памяти однозначны и покрываемы тестами.

**Проверка**
- `python -m unittest`

---

## EVO-008 — Persona Regression Suite
**Сделать**
- Добавить baseline ответы и тесты консистентности.

**Файлы**
- `tests/persona/*` (новые)

**DoD**
- Regression suite даёт стабильные baseline‑метрики.

**Проверка**
- `python -m unittest`

---

# Milestone 3 — Устойчивость и качество памяти

## EVO-009 — Self‑consistency guardrails
**Сделать**
- Проверка ответа на противоречия с ключевой памятью/KB до выдачи пользователю.

**Файлы**
- `app/services/responder/core.py`

**DoD**
- Ответы противоречащие памяти помечаются/исправляются.

**Проверка**
- `python -m unittest`

---

## EVO-010 — Memory Quality Metrics
**Сделать**
- Добавить метрики precision/recall@k и drift‑контроль.

**Файлы**
- `scripts/eval_persona.py` (новый)
- `app/emo_engine/persona/memory.py`

**DoD**
- Метрики считаются и сравнимы с baseline.

**Проверка**
- `python scripts/eval_persona.py`

---

## EVO-011 — DLQ + классификация ошибок очереди
**Сделать**
- Ввести DLQ и типизацию ошибок для API‑worker.

**Файлы**
- `app/tasks/api_worker.py`

**DoD**
- Ошибки попадают в DLQ с типами и статистикой.

**Проверка**
- `python -m unittest`

---

# Milestone 4 — Платформа персон и масштаб

## EVO-012 — Формат persona‑профиля + валидатор
**Сделать**
- Определить формат профиля и валидатор.

**Файлы**
- `docs/persona_profiles.md` (новый)
- `scripts/validate_persona_profile.py` (новый)

**DoD**
- Валидатор принимает корректный профиль и отклоняет некорректный.

**Проверка**
- `python scripts/validate_persona_profile.py <profile>`

---

## EVO-013 — Мульти‑персонный runtime
**Сделать**
- Изоляция контекста и памяти по persona‑profile.

**Файлы**
- `app/emo_engine/persona/core.py`
- `app/emo_engine/registry.py`

**DoD**
- Несколько профилей работают без деградации latency/качества.

**Проверка**
- `python -m unittest`

---

# Правило порядка выполнения (рекомендуемое)
1) EVO-001..002 (контур проверки + документация)
2) EVO-003..005 (наблюдаемость + тесты)
3) EVO-006..008 (контракт + regression suite)
4) EVO-009..011 (качество памяти + DLQ)
5) EVO-012..013 (платформа персон)
