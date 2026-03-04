# Synchatica

Synchatica — продуктовая платформа для запуска AI-персоны в нескольких каналах одновременно: Telegram-бот (личные и групповые сценарии), публичный HTTP API и фоновые воркеры для асинхронной обработки.

Проект объединяет диалоговый интеллект, персонализацию, модерацию, платежные сценарии и RAG-слой с векторным поиском.

## Что умеет продукт

### 1) Диалоги как в Telegram, так и через API
- Telegram-бот с режимами private/group/comment, командами, webhook-интеграцией и безопасной отправкой сообщений.
- HTTP API для conversation-сценариев с очередью обработки и отдельным API-worker.
- В одном запросе можно передавать текст, изображение и голос (base64 + mime), а также кастомные persona-параметры.

### 2) Персонализация AI-персоны
- Профиль персоны: имя, возраст, пол, знак зодиака, темперамент, социальность, архетипы, роль.
- Долгосрочная/краткосрочная память и контекстный выбор релевантных фрагментов.
- Выделенный `emo_engine` с реестром персон, brain/memory/ltm/states слоями.

### 3) RAG и knowledge management
- Глобальная и owner-scoped база знаний.
- Хранение векторов тегов в PostgreSQL + pgvector (`halfvec(3072)`), поиск по релевантности.
- Сервисные скрипты для bootstrap и rebuild эмбеддингов.

### 4) Модерация и безопасность контента
- Активная и passive-модерация для групп и комментариев.
- Политики по ссылкам, упоминаниям, форвардам, членству и trust-scope.
- Очереди/воркеры под модерацию, retry и наблюдаемое восстановление из processing-состояния.

### 5) Монетизация и биллинг
- Балансы бесплатных/платных запросов, резервации и компенсации.
- Telegram Stars сценарии: покупки, подарки, outbox-паттерн для надёжного применения.
- Refund outbox с lease/requeue механизмом и идемпотентным применением возвратов.

### 6) Дополнительные продуктовые сценарии
- AI-приветствия в private/group.
- Group Battle (интерактив в группах, статистика, opt-in/opt-out).
- Голосовой вывод (TTS), интеграции для TG posting/Twitter, аналитические события.

### 7) Надёжность и эксплуатация
- Идемпотентность API-запросов (Idempotency-Key), защита от конфликтов inflight.
- Rate-limit на ключ и IP, Redis-cache для API ключей, fail-safe обработка временных ошибок.
- Разделение рантайма на независимые сервисы: bot, api, workers, migrate, bootstrap-rag.

## Из чего состоит репозиторий

### Основные Python-пакеты
- `app/api` — FastAPI приложение, endpoint'ы conversation и API keys.
- `app/bot` — aiogram-бот, хендлеры private/group/moderation/battle/payments.
- `app/services` — responder pipeline и addon-сервисы (welcome, analytics, voice, battle, posting и т.д.).
- `app/emo_engine` — ядро persona-движка и управление жизненным циклом персон.
- `app/tasks` — Celery/async workers: moderation/media/payments/refunds/api/queue/scheduler.
- `app/core` — DB, Redis memory, модели, TLS, векторные и media utility.
- `app/clients` — обертки внешних клиентов (OpenAI, Telegram, HTTP, Twitter, ElevenLabs).

### Инфраструктурные папки
- `alembic/` — миграции БД.
- `scripts/` — эксплуатационные и проверочные скрипты (деплой, bootstrap RAG, CI checks).
- `docs/` — операционные и продуктовые документы.
- `tests/` — обширный набор unit/integration/regression тестов.

## Технологический стек и зависимости

### Runtime
- Python + asyncio
- aiogram (Telegram-бот)
- FastAPI + Uvicorn (HTTP API)
- Celery (фоновые задачи)
- Redis (KV/queue/cache)
- PostgreSQL + pgvector (данные и векторный поиск)
- SQLAlchemy + Alembic

### AI/ML слой
- OpenAI SDK
- NumPy/Numba
- tiktoken
- вспомогательные NLP/keyword библиотеки (`yake`, `langdetect`, `datasketch`, `flashtext`)

### Медиа и интеграции
- Pillow (обработка изображений)
- Tweepy (Twitter/X)
- ElevenLabs client (TTS-сценарии)

> Актуальные версии библиотек зафиксированы в `requirements.txt`.

## Сервисная архитектура (docker-compose)

В `docker-compose.yml` предусмотрены отдельные сервисы:
- `db`, `redis_kv`, `redis_vec`, `pgadmin`
- служебные one-shot: `migrate`, `bootstrap-rag`
- runtime: `bot`, `api`
- воркеры: `worker-tasks`, `worker-moderation`, `worker-media`, `worker-queue`, `worker-api`

Такое разделение позволяет масштабировать каналы обработки и изолировать нагрузку по типам задач.

## Проверка и качество

- Основной локальный чек: `scripts/check.sh` (pytest, compileall, ruff/pyright при наличии конфигов).
- CI/CD и деплойные контракты описаны в `docs/ci_checks.md` и `docs/deploy.md`.

## Для кого этот продукт

- Команды, которым нужен AI-ассистент в Telegram с готовыми продуктово-операционными сценариями.
- Команды, которым нужен API-доступ к той же персоне/ядру без привязки только к Telegram.
- Проекты, где критичны управляемые модерация, биллинг, observability и предсказуемая эксплуатация.
