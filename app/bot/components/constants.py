# app/bot/components/constants.py

from pathlib import Path
from app.config import settings
from redis.asyncio import Redis

LANG_FILE = Path(__file__).parent.parent / "i18n" / "welcome_messages.json"

redis_client: Redis | None = None

BOT_ID: int | None = None
BOT_USERNAME: str | None = None

WELCOME_MESSAGES: dict[str, str] = {}