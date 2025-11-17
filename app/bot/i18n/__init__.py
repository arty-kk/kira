#app/bot/i18n/__init__.py
from typing import Optional
from app.bot.i18n.menu_translation import MESSAGES
from app.bot.components.constants import redis_client
from app.config import settings

async def get_lang(user_id: int) -> str:

    raw = await redis_client.get(f"lang:{user_id}")
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode()
    return raw or settings.DEFAULT_LANG

async def t(user_id: int, key: str, **kwargs) -> str:

    lang = await get_lang(user_id)
    lang_dict = MESSAGES.get(lang, MESSAGES[settings.DEFAULT_LANG])
    template = lang_dict.get(key, MESSAGES[settings.DEFAULT_LANG].get(key, key))
    return template.format(**kwargs)