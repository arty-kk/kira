cat >app/bot/utils/keep_typing.py<< EOF
#app/bot/utils/keep_typing.py
import asyncio
import logging

from contextlib import asynccontextmanager

from app.clients.telegram_client import get_bot

logger = logging.getLogger(__name__)

bot = get_bot()

async def keep_typing(chat_id: int, stop_event: asyncio.Event) -> None:

    try:
        while not stop_event.is_set():
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Error in keep_typing")


@asynccontextmanager
async def typing_indicator(chat_id: int):
    stop = asyncio.Event()
    task = asyncio.create_task(keep_typing(chat_id, stop))
    try:
        yield
    finally:
        stop.set()
        await task
EOF