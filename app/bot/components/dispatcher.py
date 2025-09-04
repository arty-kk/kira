# app/bot/components/dispatcher.py

from aiogram import Dispatcher, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage

from app.config import settings
from app.core.memory import get_redis
from app.clients.telegram_client import get_bot
from app.services.addons.group_battle import on_battle_start, on_battle_move

if settings.DP_USE_REDIS_STORAGE:
    raw_redis = get_redis()
    redis_native = getattr(raw_redis, "_client", raw_redis)
    dp_storage = RedisStorage(redis=redis_native)
else:
    dp_storage = MemoryStorage()

bot = get_bot()
dp = Dispatcher(storage=dp_storage, bot=bot)

import app.bot.handlers

dp.callback_query.register(on_battle_start, F.data.startswith("battle_start:"))
dp.callback_query.register(on_battle_move,  F.data.startswith("battle_move:"))