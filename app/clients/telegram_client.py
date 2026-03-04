import asyncio
from contextlib import suppress

from aiogram import Bot
from aiogram.client.bot import DefaultBotProperties

from app.config import settings


_bots_by_loop: dict[int, Bot] = {}


def _loop_key() -> int:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    return id(loop)

def get_bot() -> Bot:

    key = _loop_key()
    bot = _bots_by_loop.get(key)
    if bot is None:
        bot = Bot(
            token=settings.TELEGRAM_BOT_TOKEN,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        _bots_by_loop[key] = bot
    return bot


async def close_bot_for_current_loop() -> None:
    bot = _bots_by_loop.pop(_loop_key(), None)
    if bot is None:
        return
    with suppress(Exception):
        await bot.session.close()


async def close_all_bots() -> None:
    bots = list(_bots_by_loop.values())
    _bots_by_loop.clear()
    for bot in bots:
        with suppress(Exception):
            await bot.session.close()
