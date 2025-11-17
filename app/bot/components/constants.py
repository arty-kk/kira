#app/bot/components/constants.py
from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.config import settings
from app.core.memory import get_redis, get_redis_queue, SafeRedis

LANG_FILE = Path(__file__).parent.parent / "i18n" / "welcome_messages.json"

class _LazyClient:
    def __init__(self, factory: Callable[[], SafeRedis]) -> None:
        self._factory = factory
        self._client: SafeRedis | None = None

    def _ensure(self) -> SafeRedis:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def __getattr__(self, name: str):
        return getattr(self._ensure(), name)


redis_client: SafeRedis = _LazyClient(get_redis)
redis_queue:  SafeRedis = _LazyClient(get_redis_queue)

BOT_ID: int = settings.TELEGRAM_BOT_ID
BOT_USERNAME: str = settings.TELEGRAM_BOT_USERNAME

WELCOME_MESSAGES: dict[str, str] = {}