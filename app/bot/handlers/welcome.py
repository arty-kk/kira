# app/bot/handlers/welcome.py

import asyncio
import logging
import re
import time as time_module

from aiogram import F
from aiogram.enums import ChatType, ContentType
from aiogram.types import Message, ChatMemberUpdated, User
from aiogram.filters import ChatMemberUpdatedFilter, IS_NOT_MEMBER, IS_MEMBER
from aiogram.utils.markdown import hlink
from aiogram.utils.text import quote_html

from app.clients.telegram_client import get_bot
from app.bot.components.dispatcher import dp
from app.bot.components.constants import redis_client, WELCOME_MESSAGES
from app.config import settings
from app.services.addons.welcome_manager import generate_welcome, can_greet

logger = logging.getLogger(__name__)

bot = get_bot()

@dp.message(
    F.chat.type.in_([ChatType.GROUP, ChatType.SUPERGROUP]),
    F.content_type == ContentType.NEW_CHAT_MEMBERS
)
async def on_new_members(message: Message) -> None:

    chat_id = message.chat.id
    for user in message.new_chat_members:
        uid = str(user.id)
        try:
            await redis_client.sadd(f"all_users:{chat_id}", uid)
            await redis_client.expire(f"all_users:{chat_id}", settings.MEMORY_TTL_DAYS * 86_400)
            await redis_client.sadd(f"new_users:{chat_id}", uid)
            await redis_client.expire(f"new_users:{chat_id}", settings.NEW_USER_TTL_SECONDS)
            await redis_client.set(f"last_message_ts:{chat_id}", time_module.time())
            await redis_client.expire(f"last_message_ts:{chat_id}", settings.MEMORY_TTL_DAYS * 86_400)

            logger.info("Added new member %s to chat %s", uid, chat_id)

            if not await can_greet(chat_id):
                logger.info("Rate limit reached for %s in chat %s", uid, chat_id)
                continue

            logger.info("Scheduling welcome for %s in chat %s", uid, chat_id)
            asyncio.create_task(_handle_welcome(chat_id, user))

        except Exception:
            logger.exception("Error welcoming new member %s in chat %s", uid, chat_id)


@dp.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def on_user_join_via_chat_member(update: ChatMemberUpdated) -> None:

    chat_id = update.chat.id
    user = update.new_chat_member.user
    uid = str(user.id)
    try:
        await redis_client.sadd(f"all_users:{chat_id}", uid)
        await redis_client.expire(f"all_users:{chat_id}", settings.MEMORY_TTL_DAYS * 86_400)
        await redis_client.sadd(f"new_users:{chat_id}", uid)
        await redis_client.expire(f"new_users:{chat_id}", settings.NEW_USER_TTL_SECONDS)
        await redis_client.set(f"last_message_ts:{chat_id}", time_module.time())
        await redis_client.expire(f"last_message_ts:{chat_id}", settings.MEMORY_TTL_DAYS * 86_400)

        logger.info("User %s joined chat %s via ChatMemberUpdated", uid, chat_id)

        if not await can_greet(chat_id):
            logger.info("Rate limit reached for %s in chat %s", uid, chat_id)
            return

        asyncio.create_task(_handle_welcome(chat_id, user))

    except Exception:
        logger.exception("Error in on_user_join_via_chat_member for user %s in chat %s", uid, chat_id)


async def _handle_welcome(chat_id: int, user: User) -> None:
    
    text = await generate_welcome(chat_id, user, "")
    if not text:
        safe_name = quote_html(user.full_name or user.username or str(user.id))
        mention = hlink(safe_name, f"tg://user?id={user.id}")
        text = f"Welcome {mention}! 🎉"
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        logger.exception("Failed to send HTML welcome, sending plain text")
        safe = re.sub(r"</?[\w\d]+[^>]*>", "", text)
        if safe:
            await bot.send_message(chat_id, safe)
