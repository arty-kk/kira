#app/clients/telegram_client.py
from aiogram import Bot
from aiogram.client.bot import DefaultBotProperties
from app.config import settings

_bot_singleton: Bot | None = None

def get_bot() -> Bot:

    global _bot_singleton
    if _bot_singleton is None:
        _bot_singleton = Bot(
            token=settings.TELEGRAM_BOT_TOKEN,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
    return _bot_singleton