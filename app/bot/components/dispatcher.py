#app/bot/components/dispatcher.py

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from app.config import settings
from app.core.memory import get_redis, _create_client
from app.clients.telegram_client import get_bot


def _build_storage():
    if settings.DP_USE_REDIS_STORAGE:
        try:
            raw_redis = get_redis()
            redis_native = getattr(raw_redis, "_client", raw_redis)
        except RuntimeError:
            redis_native = _create_client("default")
        return RedisStorage(redis=redis_native)
    return MemoryStorage()


bot = get_bot()
dp = Dispatcher(storage=_build_storage(), bot=bot)
