#app/bot/components/dispatcher.py

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from app.config import settings
from app.core.memory import get_redis
from app.clients.telegram_client import get_bot

if settings.DP_USE_REDIS_STORAGE:
    raw_redis = get_redis()
    redis_native = getattr(raw_redis, "_client", raw_redis)
    dp_storage = RedisStorage(redis=redis_native)
else:
    dp_storage = MemoryStorage()

bot = get_bot()
dp = Dispatcher(storage=dp_storage, bot=bot)

import app.bot.handlers