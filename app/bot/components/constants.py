#app/bot/components/constants.py
from __future__ import annotations

import inspect
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
        try:
            return getattr(self._ensure(), name)
        except RuntimeError as exc:
            if "active asyncio event loop" not in str(exc):
                raise

            async def _deferred_call(*args, **kwargs):
                target = getattr(self._ensure(), name)
                result = target(*args, **kwargs)
                if inspect.isawaitable(result):
                    return await result
                return result

            return _deferred_call


redis_client: SafeRedis = _LazyClient(get_redis)
redis_queue:  SafeRedis = _LazyClient(get_redis_queue)

BOT_ID: int = settings.TELEGRAM_BOT_ID
BOT_USERNAME: str = settings.TELEGRAM_BOT_USERNAME

WELCOME_MESSAGES: dict[str, str] = {}
