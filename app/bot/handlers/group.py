cat > app/bot/handlers/group.py << EOF
# app/bot/handlers/group.py

import asyncio
import logging
import random
import time as time_module

from datetime import datetime, timedelta, time
from typing import List

from aiogram import F, types
from aiogram.enums import ChatType, ContentType, MessageEntityType
from aiogram.types import Message

from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
import app.bot.components.constants as consts
from app.bot.components.constants import redis_client
from app.bot.handlers.moderation import handle_passive_moderation
from app.bot.utils.keep_typing import typing_indicator
from app.core.memory import record_activity, inc_msg_count
from app.tasks.message import process_message
from app.config import settings

logger = logging.getLogger(__name__)

bot = get_bot()

def _is_mention(message: types.Message) -> bool:
    raw = message.text or message.caption or ""
    if consts.BOT_USERNAME and f"@{consts.BOT_USERNAME}" in raw.lower():
        return True
    if (message.reply_to_message and consts.BOT_ID and message.reply_to_message.from_user.id == consts.BOT_ID):
        return True
    entities = (message.entities or []) + (message.caption_entities or [])
    for ent in entities:
        if ent.type == MessageEntityType.MENTION:
            mention = raw[ent.offset : ent.offset + ent.length]
            if consts.BOT_USERNAME and mention.lower() == f"@{consts.BOT_USERNAME}":
                return True
        if (ent.type == MessageEntityType.TEXT_MENTION and consts.BOT_ID and ent.user.id == consts.BOT_ID):
            return True
    return False


@dp.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.text | F.caption,
)
async def on_group_message(message: Message) -> None:

    try:
        cid = message.chat.id
        username = message.from_user.username or str(message.from_user.id)

        await redis_client.hset(f"user_map:{cid}", mapping={username: message.from_user.id})
        await redis_client.set(f"last_message_ts:{cid}", time_module.time())
        await redis_client.sadd(f"chat:{cid}:active_users", username)
        await redis_client.expire(f"chat:{cid}:active_users", settings.MEMORY_TTL_DAYS * 86_400)
        await redis_client.expire(f"last_message_ts:{cid}", settings.MEMORY_TTL_DAYS * 86_400)

        await record_activity(cid, message.from_user.id)

        if settings.ENABLE_MODERATION:
            text = message.text or message.caption or ""
            entities: List[dict] = []
            for e in message.entities or []:
                etype = e.type.value if hasattr(e.type, "value") else e.type
                entities.append({"offset": e.offset, "length": e.length, "type": etype})
            asyncio.create_task(handle_passive_moderation(cid, message, text, entities))

        if message.from_user.is_bot or not _is_mention(message):
            return

        async with typing_indicator(cid):
            today = datetime.now().date()
            key = f"daily:{cid}:{today}"
            used = int(await redis_client.get(key) or 0)
            if used >= settings.GROUP_DAILY_LIMIT:
                reset_date = (today + timedelta(days=1)).strftime("%d.%m.%Y")
                await bot.send_message(
                    cid,
                    f"{random.choice(settings.LIMIT_EXHAUSTED_PHRASES)} (resets at {reset_date})",
                    reply_to_message_id=message.message_id,
                    parse_mode="HTML",
                )
                return

            await redis_client.incr(key)
            expire_at = datetime.combine(today + timedelta(days=1), time.min)
            await redis_client.expireat(key, int(expire_at.timestamp()))

            await inc_msg_count(cid)
            payload_text = (message.text or message.caption or "").strip()
            process_message.delay(
                text=payload_text,
                chat_id=cid,
                placeholder_id=None,
                reply_to_message_id=message.message_id,
                user_id=message.from_user.id,
                username=None,
            )

    except Exception:
        logger.exception("Error in on_group_message handler")
EOF