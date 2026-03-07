# kira

`kira` — Python-проект с Telegram-ботом, HTTP API и фоновыми воркерами (Celery/Redis), объединёнными общей бизнес-логикой и модерацией.

## Что внутри

- **Bot runtime** — обработка Telegram-сообщений и модерации.
- **API runtime** — HTTP-приложение на Uvicorn.
- **Background workers** — Celery-задачи (в том числе модерация, медиа и прочие фоновые процессы).
- **Хранилища** — PostgreSQL и Redis (KV/queue/vector).

Точки входа и конфигурация запуска:
- `main.py` — общий entrypoint для bot/api в зависимости от env-переменных `RUN_BOT`/`RUN_API`.
- `docker-compose.yml` — штатный compose-стек для локального/серверного запуска.

## Требования

Минимум для запуска из Python-окружения:
- Python 3.11+
- доступные сервисы PostgreSQL и Redis
- переменные окружения (см. `.env.example`)

Обязательные Telegram-переменные при `RUN_BOT=true`:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_BOT_USERNAME`
- `TELEGRAM_BOT_ID`
- `WEBHOOK_URL`

## Быстрый старт (Docker Compose)

1. Скопируйте и заполните env:

```bash
cp .env.example .env
```

2. Поднимите стек:

```bash
docker compose up -d --build
```

3. Проверьте состояние контейнеров:

```bash
docker compose ps
```

## Локальный запуск без Compose

1. Установите зависимости:

```bash
pip install -r requirements.txt
```

2. Подготовьте env (можно отталкиваться от `.env.example`).

3. Запустите приложение:

```bash
python -u main.py
```

> По умолчанию приложение может запускать и bot, и API; это регулируется `RUN_BOT`/`RUN_API`.

## Проверки

В репозитории предусмотрен единый скрипт проверок:

```bash
bash scripts/check.sh
```

Скрипт всегда выполняет:
- `pytest -q`
- `python -m compileall app`

И условно выполняет `ruff`/`pyright`, если есть их конфиги.

## Полезные документы

- `docs/deploy.md` — контракт деплоя и эксплуатационные runbook'и.
- `docs/ci_checks.md` — какие проверки запускаются в CI.
- `docs/moderation_tools.md` — описание инструментов модерации.

