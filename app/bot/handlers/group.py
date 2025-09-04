cat > app/bot/handlers/group.py << 'EOF'
# app/bot/handlers/group.py

import asyncio
import logging
import random
import time as time_module
import json

from datetime import datetime, timedelta, time
from typing import List
from redis.exceptions import RedisError

from aiogram import F, types
from aiogram.enums import ChatType, ContentType, MessageEntityType
from aiogram.types import Message

from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
import app.bot.components.constants as consts
from app.bot.components.constants import redis_client
from app.bot.handlers.moderation import handle_passive_moderation
from app.bot.utils.debouncer import buffer_message_for_response
from app.core.memory import record_activity, inc_msg_count, MEMORY_TTL
from app.config import settings


logger = logging.getLogger(__name__)

bot = get_bot()


def _is_mention(message: types.Message) -> bool:

    if not (consts.BOT_USERNAME and consts.BOT_ID):
        return False

    expected = (consts.BOT_USERNAME or "").lower()
    raw = message.text or message.caption or ""
    entities = (message.entities or []) + (message.caption_entities or [])

    for ent in entities:
        if ent.type == MessageEntityType.MENTION:
            mention = raw[ent.offset : ent.offset + ent.length]    # включает "@"
            if mention.lstrip("@").lower() == expected:
                return True
        if ent.type == MessageEntityType.TEXT_MENTION and ent.user and ent.user.id == consts.BOT_ID:
            return True

    if expected and f"@{expected}" in raw.lower():
        return True

    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id == consts.BOT_ID

    return False


@dp.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.text | F.caption,
)
async def on_group_message(message: Message) -> None:

    try:
        cid = message.chat.id
        username = message.from_user.username or str(message.from_user.id)

        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.hset(f"user_map:{cid}", mapping={username: message.from_user.id})
            pipe.set(f"last_message_ts:{cid}", time_module.time())
            pipe.sadd(f"chat:{cid}:active_users", username)
            pipe.sadd(f"all_users:{cid}", message.from_user.id)
            pipe.expire(f"chat:{cid}:active_users", MEMORY_TTL)
            pipe.expire(f"last_message_ts:{cid}", MEMORY_TTL)
            try:
                await pipe.execute()
            except asyncio.TimeoutError:
                logger.error("Redis pipeline timeout in group handler")

        asyncio.create_task(record_activity(cid, message.from_user.id))

        if settings.ENABLE_MODERATION:
            text = message.text or message.caption or ""
            entities: List[dict] = []
            for e in message.entities or []:
                etype = e.type.value if hasattr(e.type, "value") else e.type
                entities.append({"offset": e.offset, "length": e.length, "type": etype})
            asyncio.create_task(handle_passive_moderation(cid, message, text, entities))

        is_channel_post = bool(
            (message.sender_chat and message.sender_chat.type == ChatType.CHANNEL)
            or
            (message.forward_from_chat and message.forward_from_chat.type == ChatType.CHANNEL)
        )

        if is_channel_post:
            channel_log = {
                "text": (message.text or message.caption or "").strip(),
                "message_id": message.message_id,
                "timestamp": time_module.time(),
            }
            await redis_client.lpush(
                f"mem:g:{cid}:channel_posts",
                json.dumps(channel_log, ensure_ascii=False),
            )
            await redis_client.expire(
                f"mem:g:{cid}:channel_posts",
                MEMORY_TTL,
            )

        if message.from_user and message.from_user.is_bot and not is_channel_post:
            return

        if not _is_mention(message) and not is_channel_post:
            return

        try:
            seen = await redis_client.set(
                f"seen:{cid}:{message.message_id}",
                1,
                nx=True,
                ex=43_200,
            )
            if not seen:
                logger.info("Drop duplicate group delivery chat=%s msg_id=%s", cid, message.message_id)
                return
        except Exception:
            logger.exception("failed to set seen-key in group")

        today = datetime.now().date()
        key = f"daily:{cid}:{today}"
        reset_dt = datetime.combine(today + timedelta(days=1), time.min)
        used = await redis_client.incr(key)
        if used == 1:
            await redis_client.expireat(key, int(reset_dt.timestamp()))
        if used > settings.GROUP_DAILY_LIMIT:
            reset_date = (today + timedelta(days=1)).strftime("%d.%m.%Y")
            await bot.send_message(
                cid,
                f"{random.choice(settings.LIMIT_EXHAUSTED_PHRASES)} (resets at {reset_date})",
                reply_to_message_id=message.message_id,
                parse_mode="HTML",
            )
            return

        asyncio.create_task(inc_msg_count(cid))
        await redis_client.set(
            f"msg:{cid}:{message.message_id}",
            (message.text or message.caption or "").strip(),
            ex=300
        )
        channel = message.sender_chat or message.forward_from_chat
        payload = {
            "chat_id": cid,
            "text": (message.text or message.caption or "").strip(),
            "user_id": (message.from_user.id if message.from_user else cid),
            "reply_to": message.reply_to_message and message.reply_to_message.message_id,
            "is_group": True,
            "msg_id": message.message_id,
            "is_channel_post": is_channel_post,
            "channel_id": channel.id if channel else None,
            "channel_title": getattr(channel, "title", None) if channel else None,
        }
        buffer_message_for_response(payload)

    except RedisError as e:
        logger.warning("Redis error in on_group_message, skipping noncritical ops: %s", e)
    except Exception:
         logger.exception("Error in on_group_message handler")
EOF