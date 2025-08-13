cat > app/bot/components/webhook.py << 'EOF'
# app/bot/components/webhook.py
import ssl
import asyncio
import logging
import json
import time as time_module

import aiofiles
from aiohttp import web
from aiogram import types
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramBadRequest

from app.config import settings
from app.core.memory import get_redis
from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
import app.bot.components.constants as consts
from app.bot.components.constants import (
    LANG_FILE,
    WELCOME_MESSAGES,
    redis_client,
)

logger = logging.getLogger(__name__)

bot = get_bot()

async def start_bot(stop_event: asyncio.Event | None = None) -> None:
    global redis_client, WELCOME_MESSAGES

    consts.redis_client = get_redis()
    redis_client = consts.redis_client

    me = await bot.get_me()
    consts.BOT_ID = me.id
    consts.BOT_USERNAME = me.username.lower()
    logger.info("Bot @%s starting…", consts.BOT_USERNAME)

    chat_id = settings.ALLOWED_GROUP_ID
    key = f"last_message_ts:{chat_id}"
    if not await redis_client.exists(key):
        ttl = settings.MEMORY_TTL_DAYS * 86_400
        await redis_client.set(key, time_module.time())
        await redis_client.expire(key, ttl)
        logger.debug("Initialized last_message_ts for chat %s with TTL %ds", chat_id, ttl)

    try:
        async with aiofiles.open(LANG_FILE, "r", encoding="utf-8") as f:
            WELCOME_MESSAGES.clear()
            WELCOME_MESSAGES.update(json.loads(await f.read()))
    except Exception:
        logger.exception("Failed to load welcome messages – using empty dict")
        WELCOME_MESSAGES.clear()

    try:
        cert_arg = (
            {"certificate": FSInputFile(settings.WEBHOOK_CERT)}
            if settings.USE_SELF_SIGNED_CERT
            else {}
        )
        await asyncio.wait_for(
            bot.set_webhook(
                url=settings.WEBHOOK_URL + settings.WEBHOOK_PATH,
                **cert_arg,
                drop_pending_updates=True,
                allowed_updates=[
                    "message",
                    "callback_query",
                    "chat_member",
                    "my_chat_member",
                    "inline_query",
                    "pre_checkout_query",
                ],
            ),
            timeout=60,
        )
    except asyncio.TimeoutError:
        logger.error("Timeout while setting webhook")
    except Exception:
        logger.exception("Error setting webhook")

    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain(settings.WEBHOOK_CERT, settings.WEBHOOK_KEY)

    async def handle_webhook(request: web.Request) -> web.Response:

        data = await request.json()
        update_id = data.get("update_id")

        response = web.Response(status=200)

        try:
            key = f"tg:update:{update_id}"
            seen = await redis_client.get(key)
            if not seen:
                await redis_client.set(key, "1", ex=60)
                logger.info("Incoming update: %s", data)

                upd = types.Update(**data)
                asyncio.create_task(dp.feed_update(bot, upd))
            else:
                logger.debug("Duplicate update %s skipped", update_id)
        except Exception:
            logger.exception("Error scheduling update handling")

        return response

    class IgnoreBadHttpMessage(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "BadHttpMessage" not in record.getMessage()

    logging.getLogger("aiohttp.server").addFilter(IgnoreBadHttpMessage())
    logging.getLogger("aiohttp.http_parser").addFilter(IgnoreBadHttpMessage())

    async def handle_favicon(_: web.Request) -> web.Response:
        return web.Response(status=200, text="")

    async def handle_health(_: web.Request) -> web.Response:
        return web.Response(text="OK")

    async def handle_all_other(_: web.Request) -> web.Response:
        return web.Response(status=200, text="")

    app = web.Application()
    app.router.add_get("/healthz", handle_health)
    app.router.add_post(settings.WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/favicon.ico", handle_favicon)
    app.router.add_route("*", "/{tail:.*}", handle_all_other)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner,
        host=settings.WEBHOOK_HOST,
        port=settings.WEBHOOK_PORT,
        ssl_context=ssl_context,
    )
    await site.start()
    logger.info("🚀 Webhook server running at %s%s",
                settings.WEBHOOK_URL, settings.WEBHOOK_PATH)

    _stop = stop_event or asyncio.Event()
    try:
        await _stop.wait()
    finally:
        logger.info("🛑 Shutting down webhook server")
        await runner.cleanup()
        await bot.session.close()
        logger.info("👋 Bot stopped gracefully")
EOF