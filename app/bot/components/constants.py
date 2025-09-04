# app/bot/components/constants.py
from pathlib import Path
from redis.asyncio import Redis

from app.config import settings
from app.core.memory import get_redis, get_redis_queue


LANG_FILE = Path(__file__).parent.parent / "i18n" / "welcome_messages.json"

redis_client: Redis = get_redis()
redis_queue:  Redis = get_redis_queue()

BOT_ID: int = settings.TELEGRAM_BOT_ID
BOT_USERNAME: str = settings.TELEGRAM_BOT_USERNAME

WELCOME_MESSAGES: dict[str, str] = {}